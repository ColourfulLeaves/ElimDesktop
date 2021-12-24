# -*- coding: utf-8 -*-
import logging

import re
import struct
import time

import serial
import serial.tools.list_ports

import threading
import queue


RESPONSE_OK = 0
RESPONSE_ERR = 1
RESPONSE_DAT = 2
RESPONSE_BRN = 3


def encode(value):
    if isinstance(value, bytes):
        return b"".join(("${}\r\n".format(len(value)).encode("utf-8"), value, b"\r\n"))
        pass
    elif isinstance(value, str):
        data = value.encode("utf-8")
        return b"".join(("${}\r\n".format(len(data)).encode("utf-8"), data, b"\r\n"))
        pass
    elif isinstance(value, (list, tuple)):
        data = list()
        data.append("*{}\r\n".format(len(value)).encode("utf-8"))
        for x in value:
            data.append(encode(x))
        return b''.join(data)
    else:
        return encode(str(value))


RESP_EXP_OK = re.compile(b"^\\+ok\r\n$")
RESP_EXP_ERR = re.compile(b"^-([^\r\n].*)\r\n$")
RESP_EXP_DAT = re.compile(b'^\\$(\\d+)\r\n')


def decode(data):
    # logging.info(to_decode)
    if RESP_EXP_OK.match(data):
        return RESPONSE_OK, "ok"
    m = RESP_EXP_ERR.findall(data)
    if m:
        hint = m[0]
        try:
            return RESPONSE_ERR, hint.decode("utf-8")
        except UnicodeDecodeError:
            return RESPONSE_ERR, hint
    m = RESP_EXP_DAT.findall(data)
    if m:
        body_size = int(m[0])
        head_size = len(m[0]) + 3
        pack_size = head_size + body_size + 2
        if pack_size == len(data) and data.endswith(b'\r\n'):
            return RESPONSE_DAT, data[head_size: -2]
        return RESPONSE_BRN, data
    else:
        return RESPONSE_BRN, data


p = re.compile("(\\d+\\.\\d+)C")


class MultiFuncPort(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)
        self.lock = threading.Lock()
        self.evt_cmd = threading.Event()
        self.evt_rsp = threading.Event()
        self.shutdown = threading.Event()

        self.cmd = None
        self.rsp = None

        self.port = None
        self.shutdown.clear()

    def __str__(self):
        if self.port:
            return "{}".format(self.port.portstr)
        else:
            return ""

    def disconnect(self):
        self.shutdown.set()
        self.join()

    def run(self):
        self.evt_cmd.clear()
        while True:
            try:
                while self.port:
                    if self.shutdown.is_set():
                        break
                    if self.evt_cmd.is_set():
                        self.evt_cmd.clear()
                        self.rsp = self.__execute(self.cmd, self.port)
                        self.evt_rsp.set()
                    else:
                        line = self.port.readline()
                        if line.startswith(b"# "):
                            logging.info(line)
                        elif line.startswith(b"% "):
                            logging.info(line)
            except Exception as ex:
                logging.info(ex, exc_info=True)

            if self.shutdown.is_set():
                break
            self.connect()

    def read_register(self, addr):
        cmd = encode(['read user data', struct.pack('B', addr)])
        return self.execute(cmd)

    def write_register(self, addr, value):
        if isinstance(value, float):
            cmd = encode(['write user data', struct.pack('!Bf', addr | 0x80, value)])
        else:
            cmd = encode(['write user data', struct.pack('!BL', addr | 0x80, value & 0xFFFFFFFF)])
        return self.execute(cmd)

    def firmware(self):
        cmd = encode(['firmware'])
        return self.execute(cmd)

    def reset(self):
        cmd = encode(['reset'])
        return self.execute(cmd)

    def connect(self):
        if self.port is not None:
            try:
                self.port.close()
            except serial.SerialException as ex:
                pass
            except Exception as ex:
                pass
            self.port = None

        for comport in serial.tools.list_ports.comports():
            port = None
            try:
                if comport.pid == 0x5740 and comport.vid == 0x0483:
                    # 是ST的Virtual Port Com, 尝试打开串口，读取firmware信息
                    port = serial.Serial(comport.device, baudrate=115200, timeout=0.05)
                    firmware = self.who(port).decode("utf-8")
                    if re.match(f"ver\\s+\\d.\\d,\\s+build\\s+\\S+", firmware) is not None:
                        self.port = port
                        break
            except (ValueError, serial.SerialException) as ex:
                logging.info(str(ex), exc_info=True)
                pass
            except Exception as ex:
                logging.error(str(ex), exc_info=True)
            try:
                if port:
                    port.close()
            except Exception as ex:
                logging.error(str(ex), exc_info=True)
        if port is None:
            time.sleep(5)

    def who(self, port):
        cmd = encode(['firmware'])
        resp_type, pack_data = self.__execute(cmd, port)
        if resp_type == RESPONSE_DAT:
            logging.info(pack_data)
            return pack_data
        return b"-error, unknown firmware"

    def execute(self, cmd):
        with self.lock:
            self.evt_rsp.clear()
            self.cmd = cmd
            self.rsp = (RESPONSE_ERR, "time out")
            self.evt_cmd.set()
            try:
                self.evt_rsp.wait(10.0)
                rsp = self.rsp
            except Exception as ex:
                logging.warning(ex, exc_info=True)
                rsp = None
        return rsp

    def __execute(self, cmd, port=None):
        if port is None:
            port = self.port

        logging.info(cmd)
        port.write(cmd)
        response = b''
        x = time.process_time() + 3
        while time.process_time() < x:
            if self.shutdown.is_set():
                break
            data = port.readline(1024)
            if data.startswith(b"# "):
                logging.info(data)
            elif data.startswith(b"% "):
                logging.info(data)
            elif len(data):
                # logging.info(data)
                response = response + data

                resp_type, pack_data = decode(response)
                if resp_type != RESPONSE_BRN:
                    return resp_type, pack_data
        return RESPONSE_BRN, None


class CaliBoard(object):

    def __init__(self):
        self.port = MultiFuncPort()
        self.port.start()

    def __str__(self):
        name = str(self.port)
        if name:
            return "The board has been connected via {}".format(name)
        else:
            return "Not connected"

    def disconnect(self):
        self.port.disconnect()

    def read_register(self, addr):
        return self.port.read_register(addr)

    def write_register(self, addr, value):
        return self.port.write_register(addr, value)

    def firmware(self):
        return self.port.firmware()

    def reset(self):
        return self.port.reset()


if __name__ == '__main__':
    # reset_chip()
    # save_cali_params()
    # reset_post_cali_params()
    # test_measure()
    # test_iic_addr()
    board = CaliBoard()
    board.connect()
    board.read_user_data(0x70)
    board.read_user_data(0x71)
    board.measure_env_raw()
