from abc import *
import serial
import socket
import struct
import string
import point.gemini_commands


class Gemini2Backend(ABC):
    class NotImplementedYetError(Exception): pass
    class NotSupportedError(Exception):      pass
    class ReadTimeoutError(Exception):       pass
    class ResponseError(Exception):          pass

    @abstractmethod
    def execute_one_command(self, cmd):
        pass

    @abstractmethod
    def execute_multiple_commands(self, *cmds):
        pass

    def _str_encoding(self):
        return 'ascii'


# TODO: handle serial.SerialTimeoutException (?)

class Gemini2BackendSerial(Gemini2Backend):
    def __init__(self, timeout, devname):
        self._timeout = timeout
        self._devname = devname

        # TODO: set baud to 115.2k or whatever here
        self._serial = serial.Serial(devname, timeout=self._timeout)
        self._serial.reset_input_buffer()

    def execute_one_command(self, cmd):
        if not cmd.valid_for_serial():
            raise self.NotSupportedError('command {:s} is not supported on the serial backend'.format(cmd.__class__.__name__))

        buf_cmd = cmd.encode()

        self._serial.write(buf_cmd.encode(self._str_encoding()))
        self._serial.reset_input_buffer()

        resp = cmd.response()
        if resp is None:
            return None

        buf_resp = self._wait_for_response(resp)

        len_consumed = resp.decode(buf_resp)
        if len_consumed != len(buf_resp):
            raise self.ResponseError('response was decoded, but only {:d} of the {:d} available characters were consumed'.format(len_consumed, len(buf_resp)))
        return resp

    # TODO: maybe emulate this functionality by calling execute_one_command for each cmd one at a
    # time, and then bundle up the responses and return them...?
    def execute_multiple_commands(self, *cmds):
        raise self.NotSupportedError('executing multiple commands at once is unsupported via the serial backend')

    def _wait_for_response(self, resp):
        if resp.decoder_type() == self.DecoderType.FIXED_LENGTH:
            return self._wait_for_response_fixed_length(resp.decoder())
        elif resp.decoder_type() == self.DecoderType.HASH_TERMINATED:
            return self._wait_for_response_hash_terminated(resp.decoder())
        elif resp.decoder_type() == self.DecoderType.SEMICOLON_DELIMITED:
            return self._wait_for_response_semicolon_delimited(resp.decoder())
        else:
            assert False

    def _wait_for_response_fixed_length(self, decoder):
        buf_resp = ''
        while len(buf_resp) < decoder.fixed_len():
            buf_resp += self._get_char()
            if buf_resp[-1] == '#':
                raise self.ResponseError('received \'#\' terminator as part of a fixed-length response')
        return buf_resp

    def _wait_for_response_hash_terminated(self, decoder):
        buf_resp = ''
        while not (len(buf_resp) >= 1 and buf_resp[-1] == '#'):
            buf_resp += self._get_char()
        return buf_resp

    def _wait_for_response_semicolon_delimited(self, decoder):
        buf_resp = ''
        field_count = 0
        while field_count < decoder.num_fields():
            buf_resp += self._get_char()
            if buf_resp[-1] == ';':
                field_count += 1
            elif buf_resp[-1] == '#':
                raise self.ResponseError('received \'#\' terminator as part of a semicolon-delimited response')
        return buf_resp

    def _get_char(self):
        char = self._serial.read(1).decode(self._str_encoding())
        if not char:
            raise self.ReadTimeoutError()
        return char


class Gemini2BackendUDP(Gemini2Backend):
    UDP_DEFAULT_LOCAL_ADDR  = '0.0.0.0'
    UDP_DEFAULT_LOCAL_PORT  = 11110
    UDP_DEFAULT_REMOTE_PORT = 11110

    UDP_CMD_STR_LEN_MAX = (255 - 1) # maximum length of GeminiData field, leaving one character for the terminating NULL

    UDP_RESP_DGRAM_LEN_MIN = (4 + 4 +   2) # DatagramNumber[4] + LastDatagramNumber[4] + GeminiData[  2]{   0 response chars, '#', NULL }
    UDP_RESP_DGRAM_LEN_MAX = (4 + 4 + 255) # DatagramNumber[4] + LastDatagramNumber[4] + GeminiData[255]{ 253 response chars, '#', NULL }

    UDP_RECV_BUF_SIZE = 4096 # essentially arbitrary; must be >= UDP_RESP_DGRAM_LEN_MAX

    DEFAULT_RETRY_LIMIT = 5 # how many times to attempt NACK recovery on a lost command or response datagram before giving up and raising an exception

    def __init__(self, timeout, remote_addr, local_addr=UDP_DEFAULT_LOCAL_ADDR, remote_port=UDP_DEFAULT_REMOTE_PORT, local_port=UDP_DEFAULT_LOCAL_PORT, retry_limit=DEFAULT_RETRY_LIMIT):
        self._timeout = timeout

        self._remote_addr = (remote_addr, remote_port)
        self._local_addr  = (local_addr,  local_port)

        self._retry_limit = retry_limit

        self._seqnum = 0

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(self._timeout)
        self._sock.bind(self._local_addr)

        self._stats = dict()
        self._stats['cmd_exec']      = 0
        self._stats['dgram_cmd_tx']  = 0
        self._stats['dgram_cmd_rx']  = 0
        self._stats['dgram_nack_tx'] = 0
        self._stats['dgram_nack_rx'] = 0

    def execute_one_command(self, cmd):
        if not cmd.valid_for_udp():
            raise self.NotSupportedError('command {:s} is not supported on the UDP backend'.format(cmd.__class__.__name__))

        cmd_str = cmd.encode()
        if len(cmd_str) > self.UDP_CMD_STR_LEN_MAX:
            raise ValueError('command string is too long: {:d} > {:d}'.format(len(cmd_str), self.UDP_CMD_STR_LEN_MAX))

        # if we get a response that references an earlier seqnum, it's from an earlier command and
        # we can freely discard and ignore it
        min_seqnum = self._seqnum
        skip_send = False
        # TODO: maybe receive packets in a separate thread and buffer them by their seqnum, so that
        # over here we can specifically wait on receiving a datagram with the actual seqnum we want?
        # OR: integrate the seqnum/last_seqnum processing into a separate function, so that we can
        # rapidly identify it and just immediately loop back to recv again if it's wrong

        # upon a successful NACK that indicates the command was not received, we'll end up back here
        while True:
            if not skip_send:
                cmd_seqnum = self._seqnum

                buf_cmd = struct.pack('!II', cmd_seqnum, 0)
                buf_cmd += (cmd_str).encode(self._str_encoding())
                buf_cmd += b'\x00'

                self._sock.sendto(buf_cmd, self._remote_addr)
                self._stats['dgram_cmd_tx'] += 1

            skip_send = False

            did_retry = False
            try:
                buf_resp = self._sock.recv(self.UDP_RECV_BUF_SIZE)
            except socket.timeout:
                # NOTE: our NACK handling has one edge case where it may work wrong:
                # if the original reply DOES eventually come back, but just late, then we'll probably
                # trigger the mismatched-sequence-number check exception later on
                retry_num = 0
                while True:
                    if retry_num >= self._retry_limit:
                        raise self.ReadTimeoutError('gave up after {:d} NACK retry attempts'.format(retry_num))
                    retry_num += 1
                    self._seqnum += 1
                    buf_nack = struct.pack('!IIc', self._seqnum, 0, b'\x15')
                    self._sock.sendto(buf_nack, self._remote_addr)
                    self._stats['dgram_nack_tx'] += 1
                    try:
                        buf_resp = self._sock.recv(self.UDP_RECV_BUF_SIZE)
                    except socket.timeout:
                        pass
                    else:
                        self._stats['dgram_nack_rx'] += 1
                        did_retry = True
                        break
            else:
                self._stats['dgram_cmd_rx'] += 1

            if len(buf_resp) > self.UDP_RESP_DGRAM_LEN_MAX:
                raise self.ResponseError('received UDP response datagram larger than max length: {:d} > {:d}'.format(len(buf_resp), self.UDP_RESP_DGRAM_LEN_MAX))
            elif len(buf_resp) < self.UDP_RESP_DGRAM_LEN_MIN:
                raise self.ResponseError('received UDP response datagram smaller than min length: {:d} < {:d}'.format(len(buf_resp), self.UDP_RESP_DGRAM_LEN_MIN))

            (seqnum, last_seqnum) = struct.unpack('!II', buf_resp[0:8])

            # this is a mess...
            if seqnum != self._seqnum:
                if seqnum < min_seqnum:
                    skip_send = True
                    continue
                elif seqnum < cmd_seqnum or seqnum > self._seqnum:
                    raise self.ResponseError('mismatched sequence number in UDP response datagram: {:d} != {:d}'.format(seqnum, self._seqnum))

            if did_retry:
                if last_seqnum == cmd_seqnum:
                    print('after {:d} NACK\'s, Gemini indicated that its response datagram was lost; successfully recovered'.format(retry_num))
                else:
                    print('after {:d} NACK\'s, Gemini indicated that our command datagram was lost; will resend it'.format(retry_num))
                    continue
#            else:
#                if last_seqnum != 0:
#                    # NOTE: this may or may not actually be problematic;
#                    # but the docs do say that the field should be zero in normal circumstances
#                    print('received UDP response datagram with nonzero last_seqnum {:d} in non-NACK situation (current seqnum: {:d})'.format(last_seqnum, self._seqnum))

            self._seqnum += 1

            buf_resp = buf_resp[8:].decode(self._str_encoding())

            num_nulls = buf_resp.count('\x00')
            if num_nulls == 0:
                raise self.ResponseError('received UDP response buffer of length {:d} containing no NULL terminator'.format(len(buf_resp)))
            elif num_nulls > 1:
                raise self.ResponseError('received UDP response buffer of length {:d} containing {:d} NULL characters'.format(len(buf_resp), num_nulls))
            elif buf_resp[-1] != '\x00':
                raise self.ResponseError('received UDP response buffer of length {:d} with single NULL terminator at non-end index {:d}'.format(len(buf_resp), string.rfind(buf_resp, '\x00')))
            buf_resp = buf_resp[:-1]

            resp = cmd.response()
            if len(buf_resp) == 1 and buf_resp[0] == '\x06':
                if not resp is None:
                    raise self.ResponseError('received ACK (no response), but command {:s} expected to receive response {:s}'.format(cmd.__class__.__name__, resp.__class__.__name__))
            else:
                if resp is None:
                    raise self.ResponseError('received a response of some kind, but command {:s} was expecting no response'.format(cmd.__class__.__name__))
                len_consumed = resp.decode(buf_resp)
                if len_consumed != len(buf_resp):
                    raise self.ResponseError('response was decoded, but only {:d} of the {:d} available characters were consumed'.format(len_consumed, len(buf_resp)))

            self._stats['cmd_exec'] += 1
            return resp

    def execute_multiple_commands(self, *cmds):
        # TODO: implement this!
        raise self.NotImplementedYetError('TODO')

    def _synchronously_send_and_recv(self, chars):
        # TODO: use this as the underlying function for the bulk of the common datagram handling
        # stuff in both execute_one_command and execute_multiple_commands
        raise self.NotImplementedYetError('TODO')

    def get_statistic(self, key):
        return self._stats[key]
