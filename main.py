from PyQt5.QtWidgets import QApplication, QWidget, QVBoxLayout, QPushButton, QLabel
from PyQt5.QtCore import QThread, pyqtSignal, QMutex, QMutexLocker, QWaitCondition, Qt

from PyQt5.QtGui import QImage, QPixmap


import mss
import numpy as np
from PyQt5.QtCore import QThread, pyqtSignal


'''
Credit to https://github.com/neozhaoliang/surround-view-system-introduction/blob/master/surround_view/imagebuffer.py
'''
from queue import Queue
from collections import deque  # why I need it?

import numpy as np
from PyQt5.QtCore import QSemaphore, QMutex, QWaitCondition, QMutexLocker
from PyQt5.QtCore import QMutexLocker, QWaitCondition



class Buffer(object):
    def __init__(self, size):
        # Save buffer size
        self.bufferSize = size
        # Create semaphores
        self.freeSlots = QSemaphore(self.bufferSize)
        self.usedSlots = QSemaphore(0)
        self.clearBuffer_add = QSemaphore(1)
        self.clearBuffer_get = QSemaphore(1)
        # Create mutex
        self.queueProtect = QMutex()
        # Create queue
        self.queue = Queue(self.bufferSize)

    def add(self, data, dropIfFull=False):
        # Acquire semaphore
        self.clearBuffer_add.acquire()
        # If dropping is enabled, do not block if buffer is full
        if dropIfFull:
            # Try and acquire semaphore to add item

            # Drop new frame
            # if self.freeSlots.tryAcquire():
            #     # Add item to queue
            #     self.queueProtect.lock()
            #     self.queue.put(data)
            #     self.queueProtect.unlock()
            #     # Release semaphore
            #     self.usedSlots.release()

            # Drop oldest frame
            ret = self.freeSlots.tryAcquire()
            self.queueProtect.lock()
            if not ret:
                self.queue.get()
            else:
                # Release semaphore
                self.usedSlots.release()
            self.queue.put(data)
            self.queueProtect.unlock()
        # If buffer is full, wait on semaphore
        else:
            # Acquire semaphore
            self.freeSlots.acquire()
            # Add item to queue
            self.queueProtect.lock()
            self.queue.put(data)
            self.queueProtect.unlock()
            # Release semaphore
            self.usedSlots.release()
        # Release semaphore
        self.clearBuffer_add.release()

    def get(self):
        # Acquire semaphores
        self.clearBuffer_get.acquire()
        self.usedSlots.acquire()
        # Take item from queue
        self.queueProtect.lock()
        data = self.queue.get()
        self.queueProtect.unlock()
        # Release semaphores
        self.freeSlots.release()
        self.clearBuffer_get.release()
        # Return item to caller
        return data

    def clear(self):
        # Check if buffer contains items
        if self.queue.qsize() > 0:
            # Stop adding items to buffer (will return false if an item is currently being added to the buffer)
            if self.clearBuffer_add.tryAcquire():
                # Stop taking items from buffer (will return false if an item is currently being taken from the buffer)
                if self.clearBuffer_get.tryAcquire():
                    # Release all remaining slots in queue
                    self.freeSlots.release(self.queue.qsize())
                    # Acquire all queue slots
                    self.freeSlots.acquire(self.bufferSize)
                    # Reset usedSlots to zero
                    self.usedSlots.acquire(self.queue.qsize())
                    # Clear buffer
                    for _ in range(self.queue.qsize()):
                        self.queue.get()
                    # Release all slots
                    self.freeSlots.release(self.bufferSize)
                    # Allow get method to resume
                    self.clearBuffer_get.release()
                else:
                    return False
                # Allow add method to resume
                self.clearBuffer_add.release()
                return True
            else:
                return False
        else:
            return False

    def size(self):
        return self.queue.qsize()

    def maxSize(self):
        return self.bufferSize

    def isFull(self):
        return self.queue.qsize() == self.bufferSize

    def isEmpty(self):
        return self.queue.qsize() == 0
 
class SharedImageBuffer(object):
    def __init__(self):
        # Initialize variables(s)
        self.nArrived = 0
        self.doSync = False
        self.syncSet = set()
        self.wc = QWaitCondition()
        self.imageBufferDict = dict()
        self.mutex = QMutex()

    def add(self, deviceUrl, imageBuffer, sync=False):
        # Device stream is to be synchronized
        if sync:
            with QMutexLocker(self.mutex):
                self.syncSet.add(deviceUrl)
        # Add image buffer to map
        self.imageBufferDict[deviceUrl] = imageBuffer

    def getByDeviceUrl(self, deviceUrl):
        return self.imageBufferDict[deviceUrl]

    def removeByDeviceUrl(self, deviceUrl):
        # Remove buffer for device from imageBufferDict
        self.imageBufferDict.pop(deviceUrl)

        # Also remove from syncSet (if present)
        with QMutexLocker(self.mutex):
            if self.syncSet.__contains__(deviceUrl):
                self.syncSet.remove(deviceUrl)
                self.wc.wakeAll()

    def sync(self, deviceUrl):
        # Only perform sync if enabled for specified device/stream
        self.mutex.lock()
        if self.syncSet.__contains__(deviceUrl):
            # Increment arrived count
            self.nArrived += 1
            # We are the last to arrive: wake all waiting threads
            if self.doSync and self.nArrived == len(self.syncSet):
                self.wc.wakeAll()
            # Still waiting for other streams to arrive: wait
            else:
                self.wc.wait(self.mutex)
            # Decrement arrived count
            self.nArrived -= 1
        self.mutex.unlock()

    def wakeAll(self):
        with QMutexLocker(self.mutex):
            self.wc.wakeAll()

    def setSyncEnabled(self, enable):
        self.doSync = enable

    def isSyncEnabledForDeviceUrl(self, deviceUrl):
        return self.syncSet.__contains__(deviceUrl)

    def getSyncEnabled(self):
        return self.doSync

    def containsImageBufferForDeviceUrl(self, deviceUrl):
        return self.imageBufferDict.__contains__(deviceUrl)


class ProcessingThread(QThread):
    def __init__(self, device_id, shared_buffer_manager, write_buffer, sleep_time_ms):
        super().__init__()
        self.shared_buffer_manager = shared_buffer_manager
        self.sleep_time = sleep_time_ms
        self.write_buffer = write_buffer
        self.device_id = device_id
        self.is_running = False
        self.mutex = QMutex()
        self.shared_buffer_access_mutex = QMutex()
        self.write_buffer_mutex = QMutex()


    def stop_thread(self):
        with QMutexLocker(self.mutex):
            self.is_running = False

    def run(self):
        # when
        self.is_running = True
        while self.is_running:
            self.write_buffer.sync(self.device_id)
            self.shared_buffer_access_mutex.lock()
            raw_data = self.shared_buffer_manager.getByDeviceUrl(self.device_id).get()
            raw_frame = raw_data.copy()
            self.shared_buffer_access_mutex.unlock()

            self.msleep(self.sleep_time)  
            self.write_buffer_mutex.lock()
            # do sync or not?
            self.write_buffer.getByDeviceUrl(self.device_id).add(raw_frame, True)
            self.write_buffer_mutex.unlock()
            print("read and then writing to new buffer!")


class RecorderThread(QThread):


    '''when it is run it must write to disk from buffers'''
    signal_writing_notifier = pyqtSignal()

    def __init__(self, shared_buffer_manager, device_id, width, height, filename):
        super().__init__()
        self.device_id = device_id
        self.shared_buffer_manager = shared_buffer_manager

        self.is_running = False
        self.mutex = QMutex()
        self.fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self.fps = 30.0
        self.width = width
        self.height = height
        self.filename = filename
        self.shared_buffer_access_mutex = QMutex()

    def stop_thread(self):
        with QMutexLocker(self.mutex):
            self.is_running = False

    def run(self):
        # when
        self.is_running = True
        self.writer = cv2.VideoWriter(
            self.filename,
            self.fourcc,
            self.fps,
            (self.width, self.height)
        )
        while self.is_running: # for now
            # stop or not stop mutex or isinterruptrequested
            self.shared_buffer_access_mutex.lock()
            raw_data = self.shared_buffer_manager.getByDeviceUrl(self.device_id).get()
            raw_frame = raw_data.copy()
            # if no images from sharedbuffer or stopped: break
            # or maybe just stop and break
            # self.msleep(16)  #on putpose waiting

            self.writer.write(raw_frame)

            # screen_raw_frame = self.shared_buffer_manager.getByDeviceUrl("screen").get()
            # self.writer_of_screen.write(screen_raw_frame)
            print("writing to writer")
            self.shared_buffer_access_mutex.unlock()

            self.signal_writing_notifier.emit()
            # write a frame to
        
        self.writer.release()
        # self.writer_of_screen.release()

class ScreenCaptureThread(QThread):
    screen_frame_signal = pyqtSignal(np.ndarray)

    def __init__(self, monitor_idx, screen_view, shared_buffer, device_id:str):
        super().__init__()
        self.monitor_idx = monitor_idx
        self.device_id = device_id
        self.screen_view = screen_view
        self.shared_buffer = shared_buffer
        self.is_recording = False
        self.is_running = False
        self.mutex = QMutex()   # only for own and for main thread communication!

    def run(self):
        with mss.mss() as sct:
            monitor = sct.monitors[self.monitor_idx]  # capture the primary monitor
            while self.is_running:
                # Capture screen frame
                self.shared_buffer.sync(self.device_id)
                screenshot = sct.grab(monitor)
                frame = np.array(screenshot)
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                
                self.screen_frame_signal.emit(frame)
                self.shared_buffer.getByDeviceUrl(self.device_id).add(frame, True)  #for now just raw data


                # # with QMutexLocker(self.mutex):   # it is used only if there is a shared object 
                # if self.is_recording:
                #     print("placeholder for recording screen frames")
                # print("While loop is running and condition is waiting")

                    # I have not introduced a QWaitCondition object
                
                # # Emit to the UI if recording is on and sync
                # if self.is_recording:
                #     with QMutexLocker(self.mutex):
                #         self.shared_buffer = frame
                #         self.condition.wakeAll()
                
                #     self.condition.wait(self.mutex)
    def stop_thread(self):
        with QMutexLocker(self.mutex):
            self.is_running = False
        # self.wait()
    
    def start_thread(self):
        with QMutexLocker(self.mutex):
            self.is_running = True
        
        self.start()

    def start_recording(self):
        with QMutexLocker(self.mutex):
            self.is_recording = True   #why it is not secured!?

    def stop_recording(self):
        with QMutexLocker(self.mutex):
            self.is_recording = False
        # self.wait()

    def pause_recording(self):
        with QMutexLocker(self.mutex):
            self.is_recording = False

    def resume_recording(self):
        with QMutexLocker(self.mutex):
            self.is_recording = True


import cv2
from PyQt5.QtCore import QThread, pyqtSignal

class CameraCaptureThread(QThread):
    camera_frame_signal = pyqtSignal(np.ndarray)

    def __init__(self, camera_view, shared_buffer, device_id):
        super().__init__()
        self.camera_view = camera_view
        self.shared_buffer = shared_buffer
        self.device_id = device_id
        self.mutex = QMutex()
        self.is_recording = False
        self.is_running = False
        self.capture = cv2.VideoCapture(0)  # Open default camera

    def run(self):
        while self.is_running:
            self.shared_buffer.sync(self.device_id)
            ret, frame = self.capture.read()
            if not ret:
                break
            
            self.camera_frame_signal.emit(frame)
            self.shared_buffer.getByDeviceUrl(self.device_id).add(frame, True)
            # with QMutexLocker(self.mutex):
            if self.is_recording:
                print("placeholder for recording camera frames")
                # self.condition.wakeAll()
            # self.shared_mutex.lock()
            # self.condition.wait(self.shared_mutex)
            # self.shared_mutex.unlock()
        

            # Emit to the UI if recording is on and sync with screen capture
            # if self.is_recording:
            #     with QMutexLocker(self.mutex):
            #         self.shared_buffer = frame
            #         self.condition.wakeAll()
                
            #     self.condition.wait(self.mutex)
        print("camera capture thread run() method is completed!")

    def stop_thread(self):
        with QMutexLocker(self.mutex):
            self.is_running = False
        # self.wait()
    
    def start_thread(self):
        with QMutexLocker(self.mutex):
            self.is_running = True
        
        self.start()

    def start_recording(self):
        with QMutexLocker(self.mutex):
            self.is_recording = True

    def stop_recording(self):
        with QMutexLocker(self.mutex):
            self.is_recording = False

    def pause_recording(self):
        with QMutexLocker(self.mutex):
            self.is_recording = False

    def resume_recording(self):
        with QMutexLocker(self.mutex):
            self.is_recording = True


from PyQt5.QtCore import qDebug
from PyQt5.QtGui import QImage
import numpy as np


def matToQImage(data):
    # 8-bits unsigned, NO. OF CHANNELS=1
    if data.dtype == np.uint8:
        channels = 1 if len(data.shape) == 2 else data.shape[2]
        if channels == 3:  # CV_8UC3
            # Copy input Mat
            # Create QImage with same dimensions as input Mat
            img = QImage(data, data.shape[1], data.shape[0], data.strides[0], QImage.Format_RGB888)
            return img.rgbSwapped()
        elif channels == 1:
            # Copy input Mat
            # Create QImage with same dimensions as input Mat
            img = QImage(data, data.shape[1], data.shape[0], data.strides[0], QImage.Format_Indexed8)
            return img

    qDebug("ERROR: numpy.ndarray could not be converted to QImage. Channels = %d" % data.shape[2])
    return QImage()


class MainWindow(QWidget):
    signal_stop_thread = pyqtSignal()
    signal_start_thread = pyqtSignal()
    signal_start_rec = pyqtSignal()
    signal_stop_rec = pyqtSignal()
    signal_pause_rec = pyqtSignal()
    signal_resume_rec = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Screen and Camera Capture")

        # shared buffer

        self.shared_buffer = SharedImageBuffer()
        self.final_buffer = SharedImageBuffer()
        self.final_buffer.setSyncEnabled(True)

        self.shared_buffer.setSyncEnabled(True)
        screen_device_id = "screen"
        camera_device_id = "camera"
        self.shared_buffer.add(screen_device_id, Buffer(10), True)
        self.shared_buffer.add(camera_device_id, Buffer(10), True)
        self.shared_buffer.add("screen1", Buffer(10), True)
        self.shared_buffer.add("screen2", Buffer(10), True)

        # lets say we make new shared buffer:
        self.final_buffer.add(screen_device_id, Buffer(10), True)
        self.final_buffer.add(camera_device_id, Buffer(10), True)

        # Layout setup
        self.layout = QVBoxLayout(self)
        
        self.start_threads = QPushButton("Start threads", self)
        self.stop_threads = QPushButton("Stop threads", self)

        self.start_threads.clicked.connect(self.on_start_threads)
        self.stop_threads.clicked.connect(self.on_stop_threads)
        self.layout.addWidget(self.start_threads)
        self.layout.addWidget(self.stop_threads)
        # Buttons to control recording
        self.start_button = QPushButton("Start rec", self)
        self.stop_button = QPushButton("Stop rec", self)
        self.pause_button = QPushButton("Pause rec", self)
        self.resume_button = QPushButton("Resume rec", self)

        self.start_button.clicked.connect(self.start_recording)
        self.stop_button.clicked.connect(self.stop_recording)
        self.pause_button.clicked.connect(self.pause_recording)
        self.resume_button.clicked.connect(self.resume_recording)

        self.layout.addWidget(self.start_button)
        self.layout.addWidget(self.stop_button)
        self.layout.addWidget(self.pause_button)
        self.layout.addWidget(self.resume_button)

        # Views for the screen and camera
        self.screen_view = QLabel(self)
        self.camera_view = QLabel(self)
        self.secondary_screen_view = QLabel(self)
        self.screen_view2 = QLabel(self)
        self.recording_label = QLabel(self)

        # self.screen_view.setFixedWidth(640)
        self.screen_view.setFixedHeight(200)
        # self.camera_view.setFixedWidth(640)
        self.camera_view.setFixedHeight(200)
        self.layout.addWidget(self.screen_view)
        self.layout.addWidget(self.camera_view)
        self.layout.addWidget(self.recording_label)
        self.layout.addWidget(self.secondary_screen_view)
        self.layout.addWidget(self.screen_view2)

        self.shared_mutex = QMutex()
        self.shared_condition = QWaitCondition()

        self.screen_thread = ScreenCaptureThread(1, self.screen_view, self.shared_buffer, screen_device_id)
        self.secondary_screen_thread = ScreenCaptureThread(0, self.screen_view, self.shared_buffer, screen_device_id + "1")
        self.screen_thread2 = ScreenCaptureThread(2, self.screen_view, self.shared_buffer, screen_device_id + "2")
        self.camera_thread = CameraCaptureThread(self.camera_view, self.shared_buffer, camera_device_id)

        self.recording_thread1 = RecorderThread(self.final_buffer, camera_device_id, 640, 480, "camera_test_record_fast.mp4")
        self.recording_thread2 = RecorderThread(self.final_buffer, screen_device_id, 1920, 1080, "screen_test_record_fast.mp4")


        self.processing_thread1 = ProcessingThread(camera_device_id, self.shared_buffer, self.final_buffer, sleep_time_ms=0)
        self.processing_thread2 = ProcessingThread(screen_device_id, self.shared_buffer, self.final_buffer, sleep_time_ms=0)
        self.recording_thread1.signal_writing_notifier.connect(self.display_recording)

        self.is_recording = False
        self.is_paused = False

        self.screen_thread.screen_frame_signal.connect(lambda x: self.update_frame(self.screen_view, x))
        self.camera_thread.camera_frame_signal.connect(lambda x: self.update_frame(self.camera_view, x))

        self.secondary_screen_thread.screen_frame_signal.connect(lambda x: self.update_frame(self.secondary_screen_view, x))
        self.screen_thread2.screen_frame_signal.connect(lambda x: self.update_frame(self.screen_view2, x))

        for thread in [self.camera_thread, self.screen_thread, self.secondary_screen_thread, self.screen_thread2]:
            self.signal_pause_rec.connect(thread.pause_recording)
            self.signal_resume_rec.connect(thread.resume_recording)
            self.signal_stop_rec.connect(thread.stop_recording)
            self.signal_start_rec.connect(thread.start_recording)
            self.signal_start_thread.connect(thread.start_thread)
            self.signal_stop_thread.connect(thread.stop_thread)

        self.show()
    
    def display_recording(self):
        print("recording")
        # text = self.recording_label.text()
        # if text == "":
        #     self.recording_label.setText("Recording")
        # elif text == "Recording" or text == "Recording...":
        #     self.recording_label.setText(text + ".")
        # elif text == "Recording.":
        #     self.recording_label.setText(text + ".")
        # elif text == "Recording..":
        #     self.recording_label.setText(text + ".")



    def on_stop_threads(self):
        ######## GOOD AND EASY IMPLEMENTATION #######
        self.signal_stop_thread.emit()   # NOTE: using signals will let us usually to call all threads at once. Instead of calling directly use signals
        ###### BAD IMPLEMENTATION #######
        # self.camera_thread.stop_thread()
        # self.screen_thread.stop_thread()

    def on_start_threads(self):
        self.signal_start_thread.emit()

    def update_frame(self, view, frame):
        # we can call this function, because everything is done sequentially, not in parallel here!!!
        view.setPixmap(
            QPixmap.fromImage(matToQImage(frame)).scaled(self.screen_view.width(), self.screen_view.height(), Qt.KeepAspectRatio))
    
    def start_recording(self):
        self.is_recording = True
        self.is_paused = False
        self.signal_start_rec.emit()
        self.processing_thread1.start()
        self.processing_thread2.start()
        self.recording_thread1.start()
        self.recording_thread2.start()

        self.update_view_state()

    def stop_recording(self):
        self.is_recording = False
        self.is_paused = False
        self.signal_stop_rec.emit()
        self.processing_thread1.stop_thread()
        self.processing_thread2.stop_thread()
        self.recording_thread1.stop_thread()
        self.recording_thread2.stop_thread()
        self.update_view_state()

    def pause_recording(self):
        self.is_paused = True
        self.signal_pause_rec.emit()
        self.update_view_state()

    def resume_recording(self):
        self.is_paused = False
        self.signal_resume_rec.emit()
        self.update_view_state()

    def update_view_state(self):
        # Update UI elements for recording state
        # if self.is_recording:
        #     self.start_button.setEnabled(False)
        #     self.stop_button.setEnabled(True)
        #     self.pause_button.setEnabled(True)
        #     self.resume_button.setEnabled(False)
        # else:
        #     self.start_button.setEnabled(True)
        #     self.stop_button.setEnabled(False)
        #     self.pause_button.setEnabled(False)
        #     self.resume_button.setEnabled(False)
        pass


if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)
    window = MainWindow()
    sys.exit(app.exec_())