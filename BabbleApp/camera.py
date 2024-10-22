import cv2
from cv2.typing import *
import numpy as np
import queue
import serial
import serial.tools.list_ports
import threading
import time
from lang_manager import LocaleStringManager as lang

from colorama import Fore
from config import BabbleConfig, BabbleSettingsConfig
from utils.misc_utils import get_camera_index_by_name, list_camera_names
from enum import Enum
from libscrc import rohc
import psutil, os
import sys

process = psutil.Process(os.getpid())   # Set "UNIX" thread / "WIN" process priority
try:
    sys.getwindowsversion()
except AttributeError:
    process.nice(0)  # UNIX: 20:Low, 0:Default, -20:High
else:
    process.nice(psutil.NORMAL_PRIORITY_CLASS)  # Windows: Sets process priority. If not "NORMAL" just needless scheduler overhead?!

WAIT_TIME = 0.1
BUFFER_SIZE = 32768
MAX_RESOLUTION: int = 600
# Serial communication protocol:
#  header-begin (2 bytes) "\xff\xa0"
#  header-type (2 bytes)  "\xff\xa1"
#  packet-size (2 bytes)
#  packet (packet-size bytes)
ETVR_HEADER = b"\xff\xa0\xff\xa1"
ETVR_HEADER_LEN = 6


class CameraState(Enum):
    CONNECTING: int = 0
    CONNECTED: int = 1
    DISCONNECTED: int = 2


class Camera:
    def __init__(
        self,
        config: BabbleConfig,
        camera_index: int,
        cancellation_event: "threading.Event",
        capture_event: "threading.Event",
        camera_status_outgoing: "queue.Queue[CameraState]",
        camera_output_outgoing: "queue.Queue(maxsize=2)",
        settings: BabbleSettingsConfig,
    ):
        self.camera_status = CameraState.CONNECTING
        self.config = config
        self.settings = settings
        self.camera_index = camera_index
        self.camera_list = list_camera_names()
        self.camera_status_outgoing = camera_status_outgoing
        self.camera_output_outgoing = camera_output_outgoing
        self.capture_event = capture_event
        self.cancellation_event = cancellation_event
        self.current_capture_source = config.capture_source
        self.cv2_camera: "cv2.VideoCapture" = None

        self.serial_connection = None
        self.last_frame_time = time.time()
        self.fps = 0
        self.bps = 0
        self.start = True
        self.buffer = b""
        self.sp_max = 2560  # Most ETVR frames are ~4298-4800 bytes (Keep lower!)
        self.FRAME_SIZE = [0, 0]

        self.error_message = f'{Fore.YELLOW}[{lang._instance.get_string("log.warn")}] {lang._instance.get_string("info.enterCaptureOne")} {{}} {lang._instance.get_string("info.enterCaptureTwo")}{Fore.RESET}'

    def __del__(self):
        if self.serial_connection is not None:
            self.serial_connection.close()

    def set_output_queue(self, camera_output_outgoing: "queue.Queue"):
        self.camera_output_outgoing = camera_output_outgoing

    def run(self):
        while True:
            if self.cancellation_event.is_set():
                print(
                    f'{Fore.CYAN}[{lang._instance.get_string("log.info")}] {lang._instance.get_string("info.exitCaptureThread")}{Fore.RESET}'
                )
                return
            should_push = True
            # If things aren't open, retry until they are. Don't let read requests come in any earlier
            # than this, otherwise we can deadlock (valve reference) ourselves.
            if (
                self.config.capture_source is not None
                and self.config.capture_source != ""
            ):
                if "COM" in str(self.config.capture_source):
                    if (
                        self.serial_connection is None
                        or self.camera_status == CameraState.DISCONNECTED
                        or self.config.capture_source != self.current_capture_source
                    ):
                        port = self.config.capture_source
                        self.current_capture_source = port
                        self.start_serial_connection(port)
                else:
                    if (
                        self.cv2_camera is None
                        or not self.cv2_camera.isOpened()
                        or self.camera_status == CameraState.DISCONNECTED
                        or self.config.capture_source != self.current_capture_source
                    ):
                        print(self.error_message.format(self.config.capture_source))
                        # This requires a wait, otherwise we can error and possible screw up the camera
                        # firmware. Fickle things.
                        if self.cancellation_event.wait(WAIT_TIME):
                            return

                        if self.config.capture_source not in self.camera_list:
                            self.current_capture_source = self.config.capture_source
                        else:
                            self.current_capture_source = get_camera_index_by_name(
                                self.config.capture_source
                            )

                        if self.config.use_ffmpeg:
                            self.cv2_camera = cv2.VideoCapture(
                                self.current_capture_source, cv2.CAP_FFMPEG
                            )
                        else:
                            self.cv2_camera = cv2.VideoCapture(
                                self.current_capture_source
                            )

                        if not self.settings.gui_cam_resolution_x == 0:
                            self.cv2_camera.set(
                                cv2.CAP_PROP_FRAME_WIDTH,
                                self.settings.gui_cam_resolution_x,
                            )
                        if not self.settings.gui_cam_resolution_y == 0:
                            self.cv2_camera.set(
                                cv2.CAP_PROP_FRAME_HEIGHT,
                                self.settings.gui_cam_resolution_y,
                            )
                        if not self.settings.gui_cam_framerate == 0:
                            self.cv2_camera.set(
                                cv2.CAP_PROP_FPS, self.settings.gui_cam_framerate
                            )
                        should_push = False
            else:
                # We don't have a capture source to try yet, wait for one to show up in the GUI.
                if self.cancellation_event.wait(WAIT_TIME):
                    self.camera_status = CameraState.DISCONNECTED
                    return
            # Assuming we can access our capture source, wait for another thread to request a capture.
            # Cycle every so often to see if our cancellation token has fired. This basically uses a
            # python event as a context-less, resettable one-shot channel.
            if should_push and not self.capture_event.wait(timeout=0.001):
                continue
            if self.config.capture_source is not None:
                ports = ("COM", "/dev/tty")
                if any(x in str(self.config.capture_source) for x in ports):
                    self.get_serial_camera_picture(should_push)
                else:
                    self.__del__()
                    self.get_cv2_camera_picture(should_push)
                if not should_push:
                    # if we get all the way down here, consider ourselves connected
                    self.camera_status = CameraState.CONNECTED

    def get_cv2_camera_picture(self, should_push):
        try:
            ret, image = self.cv2_camera.read()
            if not ret:
                self.cv2_camera.set(cv2.CAP_PROP_POS_FRAMES, 0)
                raise RuntimeError(lang._instance.get_string("error.frame"))
            self.FRAME_SIZE = image.shape
            frame_number = self.cv2_camera.get(cv2.CAP_PROP_POS_FRAMES)
            current_frame_time = time.time()    # Should be using "time.perf_counter()", not worth ~3x cycles?
            delta_time = current_frame_time - self.last_frame_time
            self.last_frame_time = current_frame_time
            current_fps = 1 / delta_time if delta_time > 0 else 0
            # Exponential moving average (EMA). ~1100ns savings, delicious..
            self.fps = 0.02 * current_fps + 0.98 * self.fps
            self.bps = image.nbytes * self.fps

            if should_push:
                self.push_image_to_queue(image, frame_number, self.fps)
        except Exception:
            print(
                f'{Fore.YELLOW}[{lang._instance.get_string("log.warn")}] {lang._instance.get_string("warn.captureProblem")}{Fore.RESET}'
            )
            self.camera_status = CameraState.DISCONNECTED
            pass

    def serial_read(self, rb):
        self.buffer += self.serial_connection.read(rb)
        return len(self.buffer)

    def get_next_jpeg_frame(self, conn):
        # Erm, so yah...
        buffer_len = self.serial_read(2048)
        if buffer_len >= ETVR_HEADER_LEN:
            if buffer_len > (self.sp_max * 2.3):
                # Skip frames:
                #  Ad hoc to catch up to latest frames. Got a feelin there's going to be unforeseen consequences for this one
                beg = self.buffer.rfind(ETVR_HEADER)
            else:
                beg = self.buffer.find(ETVR_HEADER)
            if beg != -1:
                self.buffer = self.buffer[beg:]
                buffer_len = len(self.buffer)
                if buffer_len >= ETVR_HEADER_LEN:
                    end = int.from_bytes(self.buffer[4:ETVR_HEADER_LEN], signed=False, byteorder="little") + ETVR_HEADER_LEN + 2
                    if conn.in_waiting >= end:
                        buffer_len = self.serial_read(conn.in_waiting)
                    if buffer_len >= end and self.buffer[end-4:end-2] == b"\xff\xd9":
                        # Compatibility w/OpenIris firmware w/o CRC
                        if self.buffer[end-2:end] != b"\xff\xa0":
                            if self.buffer[end-2:end-1] != (~rohc(self.buffer[ETVR_HEADER_LEN:end-2]) & 0xFF).to_bytes(1, "little"):
                                self.buffer = self.buffer[end:]
                                return False
                        if end > self.sp_max:
                            self.sp_max = end
                        jpeg = self.buffer[ETVR_HEADER_LEN:end-2]
                        self.buffer = self.buffer[end-2:]
                        return jpeg
                    # Sometime we end up here ~44 times in a row, because "buffer_len" < "end" or EOL '\xff\xd9' was not found. Loosing 2.3-2.5 frames before things get normal
                    if end > self.sp_max:
                        self.sp_max = end
        return False

    def get_serial_camera_picture(self, should_push):
        # Stop spamming "Serial capture source problem" if connection is lost
        if self.serial_connection is None or self.camera_status == CameraState.DISCONNECTED:
            return
        try:
            if self.serial_connection.in_waiting:
                jpeg = self.get_next_jpeg_frame(self.serial_connection)
                if jpeg:
                    # Create jpeg frame from byte string
                    image = cv2.imdecode(
                        np.fromstring(jpeg, dtype=np.uint8), cv2.IMREAD_UNCHANGED
                    )
                    if image is None:
                        print(
                            f'{Fore.YELLOW}[{lang._instance.get_string("log.warn")}] {lang._instance.get_string("warn.frameDrop")}{Fore.RESET}'
                        )
                        return
                    # Calculate the fps.
                    current_frame_time = time.time()    # Should be using "time.perf_counter()", not worth ~3x cycles?
                    delta_time = current_frame_time - self.last_frame_time
                    self.last_frame_time = current_frame_time
                    current_fps = 1 / delta_time if delta_time > 0 else 0
                    # Exponential moving average (EMA). ~1100ns savings, delicious..
                    self.fps = 0.02 * current_fps + 0.98 * self.fps
                    self.bps = len(jpeg) * self.fps

                    if should_push:
                        self.push_image_to_queue(image, current_frame_time, self.fps)
                # Discard the serial buffer. This is due to the fact that it,
                # may build up some outdated frames. A bit of a workaround here tbh.
                # Do this at the end to give buffer time to refill.
                if self.serial_connection.in_waiting >= BUFFER_SIZE:
                    print(f"{Fore.CYAN}[INFO] Discarding the serial buffer ({self.serial_connection.in_waiting} bytes){Fore.RESET}")
                    self.serial_connection.reset_input_buffer()
                    self.buffer = b''

        except Exception:
            print(
                f'{Fore.YELLOW}[{lang._instance.get_string("log.warn")}] {lang._instance.get_string("info.serialCapture")}{Fore.RESET}'
            )
            self.serial_connection.close()
            self.camera_status = CameraState.DISCONNECTED
            pass

    def start_serial_connection(self, port):
        if self.serial_connection is not None and self.serial_connection.is_open:
            # Do nothing. The connection is already open on this port.
            if self.serial_connection.port == port:
                return
            # Otherwise, close the connection before trying to reopen.
            self.serial_connection.close()
        com_ports = [tuple(p) for p in list(serial.tools.list_ports.comports())]
        # Do not try connecting if no such port i.e. device was unplugged.
        if not any(p for p in com_ports if port in p):
            return
        try:
            rate = 115200 if sys.platform == "darwin" else 3000000  # Higher baud rate not working on macOS
            conn = serial.Serial(baudrate=rate, port=port, xonxoff=False, dsrdtr=False, rtscts=False)
            # Set explicit buffer size for serial.
            conn.set_buffer_size(rx_size=BUFFER_SIZE, tx_size=BUFFER_SIZE)

            print(
                f'{Fore.CYAN}[{lang._instance.get_string("log.info")}] {lang._instance.get_string("info.ETVRConnected")} {port}{Fore.RESET}'
            )
            self.serial_connection = conn
            self.camera_status = CameraState.CONNECTED
        except Exception:
            print(
                f'{Fore.CYAN}[{lang._instance.get_string("log.info")}] {lang._instance.get_string("info.ETVRFailiure")} {port}{Fore.RESET}'
            )
            self.camera_status = CameraState.DISCONNECTED

    def clamp_max_res(self, image: MatLike) -> MatLike:
        shape = image.shape
        max_value = np.max(shape)
        if max_value > MAX_RESOLUTION:
            scale: float = MAX_RESOLUTION/max_value
            width: int = int(shape[1] * scale)
            height: int = int(shape[0] * scale)
            image = cv2.resize(image, (width, height))

            return image
        else: return image


    def push_image_to_queue(self, image, frame_number, fps):
        # If there's backpressure, just yell. We really shouldn't have this unless we start getting
        # some sort of capture event conflict though.
        qsize = self.camera_output_outgoing.qsize()
        if qsize > 1:
            print(
                f'{Fore.YELLOW}[{lang._instance.get_string("log.warn")}] {lang._instance.get_string("warn.backpressure1")} {qsize}. {lang._instance.get_string("warn.backpressure2")}{Fore.RESET}'
            )
        self.camera_output_outgoing.put((self.clamp_max_res(image), frame_number, fps))
        self.capture_event.clear()
