import array
import binascii
import copy
import datetime
import json
import logging
import sqlite3
import struct
import sys, os, random

import threading
import time
import re
from http.server import HTTPServer, BaseHTTPRequestHandler, SimpleHTTPRequestHandler
import http
import urllib

import yaml
from PyQt5.QtCore import *
from PyQt5.QtGui import *
from PyQt5.QtWidgets import *

import matplotlib

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qt5 import NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
import matplotlib.dates as mdates
import matplotlib.dates as mdates

import Board

matplotlib.use('Qt5Agg')


class MyHTTPRequestHandler(SimpleHTTPRequestHandler):

    def do_GET(self):
        """Serve a GET request."""
        logging.info(self.path)
        r = urllib.parse.urlparse(self.path)
        self.path = r.path
        queries = urllib.parse.parse_qs(r.query)
        handlers = {'/measure': self.on_measure, '/program': self.on_program, "/unlock": self.on_unlock,
                    "/register": self.on_register, '/data': self.on_data}
        f = handlers.get(self.path, None)
        if f:
            f(queries)
            return

        if self.path == r'/watch':
            json_text = self.server.owner.json_text()
            self.send_response(http.HTTPStatus.OK)
            self.send_header("Content-type", "application/json;charset=utf-8")
            self.send_header("Content-Length", str(len(json_text)))
            self.end_headers()
            self.wfile.write(json_text.encode("utf-8"))
            return

        f = self.send_head()
        if f:
            try:
                self.copyfile(f, self.wfile)
            finally:
                f.close()

    def on_measure(self, queries):
        board = self.server.owner.board

        json_text = json.dumps(board.read_temperature())
        self.send_response(http.HTTPStatus.OK)
        self.send_header("Content-type", "application/json;charset=utf-8")
        self.send_header("Content-Length", str(len(json_text)))
        self.end_headers()
        self.wfile.write(json_text.encode("utf-8"))

    def on_register(self, queries):
        board = self.server.owner.board
        try:
            addr = MyHTTPRequestHandler.number(queries['addr'][0])
            val_list = queries.get("val")
            if val_list is None:
                rsp = board.read_register(addr)
            else:
                val = MyHTTPRequestHandler.number(val_list[0])
                rsp = board.write_register(addr, val)
        except Exception as ex:
            rsp = {'error': str(ex)}
        json_text = json.dumps(rsp)
        self.send_response(http.HTTPStatus.OK)
        self.send_header("Content-type", "application/json;charset=utf-8")
        self.send_header("Content-Length", str(len(json_text)))
        self.end_headers()
        self.wfile.write(json_text.encode("utf-8"))

    def on_program(self, queries):
        board = self.server.owner.board

        json_text = json.dumps(board.program())
        self.send_response(http.HTTPStatus.OK)
        self.send_header("Content-type", "application/json;charset=utf-8")
        self.send_header("Content-Length", str(len(json_text)))
        self.end_headers()
        self.wfile.write(json_text.encode("utf-8"))

    def on_unlock(self, queries):
        board = self.server.owner.board

        key = MyHTTPRequestHandler.number(queries['key'][0])
        json_text = json.dumps(board.unlock(key))
        self.send_response(http.HTTPStatus.OK)
        self.send_header("Content-type", "application/json;charset=utf-8")
        self.send_header("Content-Length", str(len(json_text)))
        self.end_headers()
        self.wfile.write(json_text.encode("utf-8"))

    def on_data(self, queries):
        board = self.server.owner.board

        json_text = json.dumps(board.data())
        self.send_response(http.HTTPStatus.OK)
        self.send_header("Content-type", "application/json;charset=utf-8")
        self.send_header("Content-Length", str(len(json_text)))
        self.end_headers()
        self.wfile.write(json_text.encode("utf-8"))

    @staticmethod
    def number(text):
        text = text.strip()
        text = text.lower()
        if text.find("0x") >= 0:
            n = int(text, 16)
        else:
            try:
                n = int(text)
            except ValueError as ex:
                n = float(text)
        return n


class ServerThread(threading.Thread):
    """reads temperature from board"""

    def __init__(self, owner):
        threading.Thread.__init__(self)
        self.owner = owner
        self.server = HTTPServer(('0.0.0.0', 8902), MyHTTPRequestHandler)
        setattr(self.server, "owner", owner)

    def run(self):
        self.server.serve_forever(poll_interval=0.5)


class BoardThread(threading.Thread):
    """ """

    def __init__(self, owner, polling_time):
        threading.Thread.__init__(self)
        self.owner = owner
        self.polling_time = polling_time
        self.lock = threading.Lock()
        self.terminate_flag = False
        self.evt = threading.Event()

        self.times = []
        self.obj_temperatures = []
        self.env_temperatures = []
        self.ntc_values = []
        self.inf_values = []
        self.ntc_ohms = []
        self.inf_mvs = []

        measurements = self.owner.conf.get("Measurement", {})
        self.measure_inf = measurements.get("Inf", True)
        self.measure_ntc = measurements.get("Ntc", True)
        self.measure_ohm = measurements.get("Ohm", True)
        self.measure_mv = measurements.get("Mv", True)

        self.last_measure_time = time.time()

        self.cali_board = Board.CaliBoard()

        self.evt.clear()

    def shutdown(self):
        self.cali_board.disconnect()

        self.terminate_flag = True
        self.evt.set()
        self.join()

    def run(self):
        timeout = 0.5
        while not self.terminate_flag:
            self.evt.clear()
            self.evt.wait(timeout)
            logging.info(f"wake up, evt:{self.evt.is_set()}")
            if self.terminate_flag:
                break
            self.evt.set()
            logging.info("do something")
            self.measure()
            self.owner.measure_done.emit()
            logging.info(f"in run last_measure_time {self.last_measure_time}")
            logging.error(f"took {time.time() - self.last_measure_time}s to measure last time")
            timeout = max(0.1, self.polling_time - (time.time() - self.last_measure_time))
            logging.info(f"timeout for next measurement {timeout}")

        logging.info("BoardThread ends")

    def program(self):
        return self.write_register(0xEE, 00)

    def unlock(self, key):
        return self.write_register(0xEF, key)

    def measure(self):
        try:
            t = time.time()
            logging.info(f"last_measure_time {t}")

            result = self.read_register(0x60)
            logging.info("measure 0x60 done")
            env, obj = result['val']["short"]

            if self.measure_inf:
                result = self.read_register(0x63)
                logging.info("measure 0x63 done")
                inf = round(result['val']["float"], 3)
            else:
                inf = 0

            if self.measure_ntc:
                result = self.read_register(0x64)
                logging.info("measure 0x64 done")
                ntc = result['val']["int"]
            else:
                ntc = 0

            if self.measure_mv:
                result = self.read_register(0x66)
                logging.info("measure 0x66 done")
                mv = result['val']['float']
            else:
                mv = 0

            if self.measure_ohm:
                result = self.read_register(0x65)
                logging.info("measure 0x65 done")
                ohm = result['val']["float"]
                logging.info(f"ohm: {ohm}")

            else:
                ohm = 0

            self.last_measure_time = t

            with self.lock:
                t = time.time()
                logging.error(f"measure end time: {datetime.datetime.fromtimestamp(t)}")
                self.times.append(t)
                self.obj_temperatures.append(obj / 100)
                self.env_temperatures.append(env / 100)
                self.inf_values.append(inf)
                self.ntc_values.append(ntc)
                self.ntc_ohms.append(ohm)
                self.inf_mvs.append(mv)

                if len(self.times) > 200:
                    self.times.pop(0)
                    self.obj_temperatures.pop(0)
                    self.env_temperatures.pop(0)
                    self.inf_values.pop(0)
                    self.ntc_values.pop(0)
                    self.ntc_ohms.pop(0)
                    self.inf_mvs.pop(0)

        except Exception as ex:
            logging.info(ex, exc_info=True)

    @property
    def last_measurement(self):
        logging.info("enter last_measurement")
        with self.lock:
            if len(self.times) > 0:
                t = time.time()
                if t - self.times[-1] < 0.8:
                    # 有足够新的数据，直接采用刚刚读取到的树
                    data = {'obj': self.obj_temperatures[-1], 'env': self.env_temperatures[-1],
                            'inf': self.inf_values[-1], 'ntc': self.ntc_values[-1], 'ohm': self.ntc_ohms[-1],
                            'mv': self.inf_mvs[-1], 'tim': str(datetime.datetime.fromtimestamp(self.times[-1]))}
                    logging.info("leave last_measurement with data")
                    return data
        logging.info("leave last_measurement")
        return None

    def read_temperature(self):
        data = self.last_measurement
        if data:
            return data
        logging.info("notify to measure")
        self.evt.set()
        while True:
            if self.evt.is_set():
                time.sleep(0.05)
            else:
                logging.info("measure done")
                data = self.last_measurement
                return data

    def read_register(self, addr):
        result = {}
        error = ""
        for x in range(3):
            try:
                resp_type, resp_data = self.cali_board.read_register(addr)
                result['response'] = {Board.RESPONSE_OK: 'ok', Board.RESPONSE_DAT: 'data', Board.RESPONSE_ERR: "error",
                                      Board.RESPONSE_BRN: 'broken'}.get(resp_type)
                if resp_type == Board.RESPONSE_ERR:
                    error = resp_data
                else:
                    error = ""
                if resp_type == Board.RESPONSE_DAT:
                    result['val'] = BoardThread.interpret_response_data(resp_data)
                break
            except Exception as ex:
                error = str(ex)
        if error != '':
            result['error'] = error

        logging.info(result)
        return result

    def write_register(self, addr, val):
        result = {}
        error = ""
        for x in range(3):
            try:
                resp_type, resp_data = self.cali_board.write_register(addr, val)
                result['response'] = {Board.RESPONSE_OK: 'ok', Board.RESPONSE_DAT: 'data', Board.RESPONSE_ERR: "error",
                                      Board.RESPONSE_BRN: 'broken'}.get(resp_type)
                if resp_type == Board.RESPONSE_ERR:
                    error = resp_data
                else:
                    error = ""
                if resp_type == Board.RESPONSE_DAT:
                    result['val'] = BoardThread.interpret_response_data(resp_data)
                break
            except Exception as ex:
                error = str(ex)
        if error != '':
            result['error'] = error
        return result

    def data(self):
        with self.lock:
            rsp = {"env": copy.copy(self.env_temperatures), "inf": copy.copy(self.inf_values),
                   "ntc": copy.copy(self.ntc_values), "obj": copy.copy(self.obj_temperatures),
                   "ohm": copy.copy(self.ntc_ohms), "mv": copy.copy(self.inf_mvs),
                   "ts": copy.copy(self.times)}
        return rsp

    @staticmethod
    def interpret_response_data(resp_data):
        result = {"raw": {"bin": str(resp_data), "hex": binascii.hexlify(resp_data).decode("utf-8")}}
        if len(resp_data) > 4:
            result['float'] = struct.unpack_from(">f", resp_data, 1)[0]
            result["int"] = struct.unpack_from(">i", resp_data, 1)[0]
        if len(resp_data) > 1:
            a = []
            for x in resp_data:
                a.append(x)
            del a[0]
            result['char'] = a

        if len(resp_data) > 3:
            a = []
            offset = 1
            try:
                while True:
                    a.extend(struct.unpack_from('!h', resp_data, offset))
                    offset += 2
            except Exception as ex:
                pass
            result['short'] = a

        if len(resp_data) > 5:
            result["checksum"] = resp_data[5]
        if len(resp_data) > 0:
            result["status"] = resp_data[0]
        return result


class AppForm(QMainWindow):
    measure_done = pyqtSignal()

    def __init__(self, parent=None):
        QMainWindow.__init__(self, parent)
        try:
            with open("elim.conf") as f:
                self.conf = yaml.safe_load(f)
        except FileNotFoundError as ex:
            logging.error(ex, exc_info=True)
            self.conf = {}

        self.setWindowTitle(self.conf.get("Title", 'Elim'))
        self.resize(QDesktopWidget().availableGeometry(self).size() * 0.7)

        # create menu
        self.file_menu = self.menuBar().addMenu("&File")

        load_file_action = self.create_action("&Save plot",
                                              shortcut="Ctrl+S", slot=self.save_plot,
                                              tip="Save the plot")
        quit_action = self.create_action("&Quit", slot=self.close,
                                         shortcut="Ctrl+Q", tip="Close the application")

        self.add_actions(self.file_menu,
                         (load_file_action, None, quit_action))

        self.help_menu = self.menuBar().addMenu("&Help")
        about_action = self.create_action("&About",
                                          shortcut='F1', slot=self.on_about,
                                          tip='About the demo')

        self.add_actions(self.help_menu, (about_action,))

        # create status bar
        self.status_text = QLabel("")
        self.statusBar().addWidget(self.status_text, 2)

        self.pid_text = QLabel("")
        self.statusBar().addWidget(self.pid_text, 1)

        self.range_text = QLabel("")
        self.statusBar().addWidget(self.range_text, 1)

        self.y_range_0 = None
        self.y_range_1 = None
        self.apply_button = None

        self.text_palette_red = QPalette()
        self.text_palette_red.setColor(QPalette.ColorRole.Text, QColor(0xFF, 0, 0))

        self.text_palette_blue = QPalette()
        self.text_palette_blue.setColor(QPalette.ColorRole.Text, QColor(0xFF, 0, 0))

        self.create_main_frame()

        self.measure_done.connect(self.on_draw)

        self.board = BoardThread(self, self.conf.get("PollingTime", 1))
        self.board.start()

        # self.on_draw()

        self.http_thread = ServerThread(self)
        self.http_thread.start()

        self.my_timer = QTimer(self)
        self.my_timer.timeout.connect(self.my_timer_cb)
        self.my_timer.start(100)
        self.update_count = 0

    def my_timer_cb(self):
        self.update_count += 1
        if self.update_count > 2:
            self.update_count = 0
            clr = self.text_palette_red.color(QPalette.ColorRole.Text)
            blue = 255 - (255 - clr.blue()) * 0.8
            green = 255 - (255 - clr.green()) * 0.8
            clr.setBlue(blue)
            clr.setGreen(green)
            self.text_palette_red.setColor(QPalette.ColorRole.Text, clr)
            self.y_range_0.setPalette(self.text_palette_red)

            clr = self.text_palette_blue.color(QPalette.ColorRole.Text)
            red = 255 - (255 - clr.red()) * 0.8
            green = 255 - (255 - clr.green()) * 0.8
            clr.setRed(red)
            clr.setGreen(green)
            self.text_palette_blue.setColor(QPalette.ColorRole.Text, clr)
            self.y_range_1.setPalette(self.text_palette_blue)


    def closeEvent(self, event) -> None:
        self.board.shutdown()
        self.http_thread.server.shutdown()
        event.accept()

    def save_plot(self):
        file_choices = "PNG (*.png)|*.png"

        path = QFileDialog.getSaveFileName(self,
                                           'Save file', '',
                                           file_choices)
        if path:
            self.canvas.print_figure(path, dpi=self.dpi)
            self.statusBar().showMessage('Saved to %s' % path, 2000)

    def on_about(self):
        msg = """ A demo of using PyQt with matplotlib:

         * Use the matplotlib navigation bar
         * Add values to the text box and press Enter (or click "Draw")
         * Show or hide the grid
         * Drag the slider to modify the width of the bars
         * Save the plot to a file using the File menu
         * Click on a bar to receive an informative message
        """
        QMessageBox.about(self, "About the demo", msg.strip())

    def on_pick(self, event):
        # The event received here is of the type
        # matplotlib.backend_bases.PickEvent
        #
        # It carries lots of information, of which we're using
        # only a small amount here.
        #
        box_points = event.artist.get_bbox().get_points()
        msg = "You've clicked on a bar with coords:% s" % box_points

        QMessageBox.information(self, "Click!", msg)

    def on_draw(self):
        """ Redraws the figure
        """
        try:
            # clear the axes and redraw the plot anew
            #
            self.axes.clear()
            # self.axes.grid(self.grid_cb.isChecked())

            # 设置时间轴显示格式
            # hour_locator = mdates.HourLocator((0, 11, 12,))  # 只显示0、12时
            minute_locator = mdates.MinuteLocator((0, 20, 40))
            self.axes.xaxis.set_major_locator(minute_locator)
            formatter = mdates.ConciseDateFormatter(minute_locator,
                                                    formats=['%H:%M', '%H:%M', '%H:%M', '%H:%M', '%H:%M',
                                                             '%H:%M'])  # 显示格式
            # formatter.formats[3] = '%H'
            # formatter.zero_formats = [''] + formatter.formats[:-1]
            self.axes.xaxis.set_major_formatter(formatter)
            # self.axes.set_ylim(round(self.temperature, 2) - 0.02, round(self.temperature, 2) + 0.02)

            x = []
            with self.board.lock:
                for t in self.board.times:
                    x.append(datetime.datetime.fromtimestamp(t))
                self.axes.plot(x, self.board.obj_temperatures, ".-", color="red", label="To", linewidth=0.2, ms=0.25)
                self.axes.plot(x, self.board.env_temperatures, ".-", color="blue", label="Te", linewidth=0.2, ms=0.25)

                self.y_range_0.setText(f"{self.board.obj_temperatures[-1]:.2f}")
                self.y_range_1.setText(f"{self.board.env_temperatures[-1]:.2f}")
            self.axes.legend()
            self.canvas.draw()

            self.update_count = 0
            self.text_palette_red.setColor(QPalette.ColorRole.Text, QColor(0xFF, 0, 0))
            self.y_range_0.setPalette(self.text_palette_red)
            self.text_palette_blue.setColor(QPalette.ColorRole.Text, QColor(0, 0, 0xFF))
            self.y_range_1.setPalette(self.text_palette_blue)
            logging.info("update text")

        except Exception as ex:
            logging.error(ex, exc_info=True)

    def create_main_frame(self):
        self.main_frame = QWidget()

        # Create the mpl Figure and FigCanvas objects.
        # 5x4 inches, 100 dots-per-inch
        #
        self.dpi = 200
        self.fig = Figure((5.0, 4.0), dpi=self.dpi)
        self.canvas = FigureCanvas(self.fig)
        self.canvas.setParent(self.main_frame)

        # Since we have only one plot, we can use add_axes
        # instead of add_subplot, but then the subplot
        # configuration tool in the navigation toolbar wouldn't
        # work.
        #
        self.axes = self.fig.add_subplot(111)

        # Bind the 'pick' event for clicking on one of the bars
        #
        self.canvas.mpl_connect('pick_event', self.on_pick)

        # Create the navigation toolbar, tied to the canvas
        #
        self.mpl_toolbar = NavigationToolbar(self.canvas, self.main_frame)

        # Other GUI controls
        #
        range_label = QLabel("目标温度(To) :")

        self.y_range_0 = QLineEdit()
        self.y_range_0.setMinimumWidth(20)
        self.y_range_0.setReadOnly(True)
        self.y_range_0.setFont(QFont("Times", 18, QFont.Bold))
        self.y_range_0.setPalette(self.text_palette_red)

        line_label = QLabel(" 环境温度(Te) :")

        self.y_range_1 = QLineEdit()
        self.y_range_1.setMinimumWidth(20)
        self.y_range_1.setReadOnly(True)
        self.y_range_1.setFont(QFont("Times", 18, QFont.Bold))

        #
        # Layout with box sizers
        #
        hbox = QHBoxLayout()

        for w in [range_label, self.y_range_0, line_label, self.y_range_1]:
            hbox.addWidget(w)
            hbox.setAlignment(w, Qt.AlignVCenter)

        vbox = QVBoxLayout()
        vbox.addWidget(self.mpl_toolbar)
        vbox.addWidget(self.canvas)

        vbox.addLayout(hbox)

        self.main_frame.setLayout(vbox)
        self.setCentralWidget(self.main_frame)

    def add_actions(self, target, actions):
        for action in actions:
            if action is None:
                target.addSeparator()
            else:
                target.addAction(action)

    def create_action(self, text, slot=None, shortcut=None,
                      icon=None, tip=None, checkable=False,
                      signal="triggered()"):
        action = QAction(text, self)
        if icon is not None:
            action.setIcon(QIcon("icons/{0}.png".format(icon)))
        if shortcut is not None:
            action.setShortcut(shortcut)
        if tip is not None:
            action.setToolTip(tip)
            action.setStatusTip(tip)
        if slot is not None:
            action.triggered.connect(slot)
        if checkable:
            action.setCheckable(True)
        return action


def init_logging():
    # 创建一个logger
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)  # Log等级总开关

    # 创建一个handler，用于写入日志文件
    rq = time.strftime('%Y%m%d %H%M', time.localtime(time.time()))
    log_path = os.path.join(os.path.dirname(sys.argv[0]), 'Logs')
    os.makedirs(log_path, exist_ok=True)
    log_name = os.path.sep.join((log_path, rq + '.log'))
    logfile = log_name
    fh = logging.FileHandler(logfile, mode='w')
    fh.setLevel(logging.DEBUG)  # 输出到file的log等级的开关

    # 定义handler的输出格式
    formatter = logging.Formatter(
        "%(asctime)s - ln:%(lineno)d- %(funcName)s - %(levelname)s: %(message)s")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    chlr = logging.StreamHandler()  # 输出到控制台的handler
    chlr.setFormatter(formatter)
    logger.addHandler(chlr)


if __name__ == "__main__":
    init_logging()
    app = QApplication(sys.argv)
    form = AppForm()
    form.show()
    app.exec_()
