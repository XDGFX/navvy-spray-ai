#!/usr/bin/env python3
"""
vision/vision.py

The camera-specific module; takes images and returns detected plants.

Callum Morrison, 2021
"""

import json
import os
import re
import time
from pathlib import Path
from queue import Queue

import cv2
import numpy as np
import redis
import requests
from dotenv import load_dotenv
from picamera import PiCamera
from picamera.array import PiRGBArray
from pymata4 import pymata4

import logs

# Setup log
log = logs.create_log(__name__)

# Load environment variables
env_path = Path(__file__).parent.absolute() / '.env'
load_dotenv(dotenv_path=env_path)

# Initialise Redis
redis_url = re.match('([\d.]+):([\d]+)',
                     os.environ.get('REDIS_URL')).groups()
r = redis.Redis(host=redis_url[0], port=redis_url[1], db=0)


class Camera():
    def __init__(self, sid):
        self.movement_key = f'movement--{sid}'

        if os.environ.get('DEBUG_CAM').lower() in ['true', 't', '1']:
            self.cam = cv2.VideoCapture(str(
                Path(__file__).parent.absolute() / '0000.mkv'))
        else:
            # self.cam = cv2.VideoCapture(0)
            self.cam = PiCamera()
            self.cam.resolution = (640, 480)
            self.raw_cap = PiRGBArray(self.cam, size=(640, 480))

        self.active = False

        self.frame_buffer = Queue()
        self.clear_buffer()

    def clear_buffer(self):
        """
        Clears frame and movement buffers.
        """
        self.frame_buffer.queue.clear()
        self.first_frame = None
        self.prev_frame = None

        r.set(self.movement_key, json.dumps((0, 0, 0)))

    def start_capture(self):
        """
        Start scheduled capture from the camera at a defined framerate.
        """
        log.info('Starting frame capture.')

        self.active = True

        start_time = time.time()
        frame_wait = 1 / int(os.environ.get('FRAMERATE_TRACK'))

        while self.active:
            # Capture frame
            self.cam.capture(self.raw_cap, format='bgr')
            frame = self.raw_cap.array

            # Clear frame buffer for next frame
            self.raw_cap.truncate(0)

            # Check frame was correctly read
            if frame is None:
                if os.environ.get('DEBUG_CAM').lower() in ['true', 't', '1']:
                    # Reload the video stream
                    self.cam = cv2.VideoCapture(str(
                        Path(__file__).parent.absolute() / '0000.mkv'))

                    continue

                else:
                    raise Exception('Unable to capture a frame!')

            # If first capture, save to first_frame, otherwise add to frame buffer
            if self.first_frame is None:
                self.first_frame = frame
            else:
                self.frame_buffer.put(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))

            # Wait until next frame should be captured
            time.sleep(frame_wait - ((time.time() - start_time) % frame_wait))

    def start_track(self):
        """
        Track movement between each frame in the frame buffer using Lucas-Kanade Optical Flow.
        """
        self.track_err_count = 0

        while self.active:
            # Make sure queue doesn't get too long
            queue_length = len(self.frame_buffer.queue)
            if queue_length > int(os.getenv('FRAMERATE_TRACK')):
                log.warning(
                    f'Length of frame queue is getting long ({queue_length})!. Check that the processor is not overwhelmed.')

            # Make sure track error count is not too long
            if self.track_err_count >= 3:
                log.error(
                    f'Unable to track {self.track_err_count} frames in a row! There may be an issue with the camera')

            # If frame_buffer was recently cleared, wait for re-initialisation
            while (self.first_frame is None) or (len(self.frame_buffer.queue) == 0):
                time.sleep(0.01)

            # Setup frames
            new_frame = self.frame_buffer.get()
            prev_frame = self.prev_frame if self.prev_frame is not None else cv2.cvtColor(
                self.first_frame, cv2.COLOR_BGR2GRAY)

            # Setup track points
            prev_pts = cv2.goodFeaturesToTrack(prev_frame,
                                               maxCorners=200,
                                               qualityLevel=0.01,
                                               minDistance=30,
                                               blockSize=3)

            # Calculate optical flow
            new_pts, status, err = cv2.calcOpticalFlowPyrLK(
                prev_frame, new_frame, prev_pts, None)

            # Sanity check
            assert prev_pts.shape == new_pts.shape

            # Filter only valid points
            idx = np.where(status == 1)[0]
            prev_pts = prev_pts[idx]
            new_pts = new_pts[idx]

            try:
                # Find transformation matrix
                m = cv2.estimateAffinePartial2D(
                    prev_pts, new_pts)

                # Extract traslation
                dx = m[0][0][2]
                dy = m[0][1][2]

                # Extract rotation angle
                da = np.arctan2(m[0][1][0], m[0][0][0])

                # log.debug(
                #     f'Movement: {dx:5.2f}:{dy:5.2f}:{da:5.2f}')

                old_movement = json.loads(r.get(self.movement_key))
                new_pos = tuple(
                    map(lambda i, j: i + j, old_movement, (dx, dy, da)))

                r.set(self.movement_key, json.dumps(new_pos))

                self.track_err_count = 0
                self.prev_frame = new_frame

            except Exception as e:
                log.debug('Failed to calculate transformation', e)
                self.track_err_count += 1

    def get_inference(self, img):
        """
        Send an image to the inference server and return the result.

        @return
        bbox        json list of bounding boxes
        """
        url = os.getenv('INFERENCE_URL') + '/api/detect'

        # Prepare headers for http request
        headers = {'content-type': 'image/jpeg'}

        # Encode image as jpeg
        _, img_encoded = cv2.imencode('.jpg', img)

        # Send http request with image and receive response
        try:
            response = requests.post(
                url,
                data=img_encoded.tostring(),
                headers=headers,
                timeout=int(os.getenv('INFERENCE_TIMEOUT')) / 1000
            )

            return json.loads(response.text)

        except requests.exceptions.ReadTimeout as e:
            log.debug(e)

            return None

    def draw_bounding_boxes(self, img, bbox):
        """
        Draw rectangles around each detected instance in img.

        @return
        img         modified image
        """
        for i in range(bbox['count']):
            box = bbox['bounding_boxes'][i]
            x = int(box[0])
            y = int(box[1])
            w = int(box[2])
            h = int(box[3])

            img = cv2.rectangle(img, (x, y), (x+w, y+h), (0, 0, 255), 2)

        return img

    def write_image(self, img, filename):
        """
        Write an OpenCV image to a file for debugging.
        """
        cv2.imwrite(filename, img)


class Servo():
    def __init__(self, sid, img_width, img_height):

        # Setup general parameters
        self.movement_key = f'movement--{sid}'
        self.img_width = img_width
        self.img_height = img_height

        # Arduino setup
        self.a = pymata4.Pymata4()
        self.servo_pin_x = 9
        self.servo_pin_y = 10
        self.spray_pin = 5

        # Servo parameters
        self.spray_per_plant = float(os.environ.get(
            'SPRAY_PER_PLANT')) or 0.25     # seconds
        self.spray_total_time = float(os.environ.get(
            'SPRAY_TOTAL_TIME')) or 3       # seconds
        self.spray_angle_rate = float(os.environ.get(
            'SPRAY_ANGLE_RATE')) or 240     # degrees per second

        # Multiplier to get from distance to angle, may change this to tan(angle) = dist / height
        self.spray_dist2angle = float(os.environ.get(
            'SPRAY_DIST2ANGLE')) or 5

        # Setup Arduino output and set to vertical
        for pin in [self.servo_pin_x, self.servo_pin_y]:
            self.a.set_pin_mode_servo(pin)
            self.a.servo_write(pin, 90)
            time.sleep(90 / self.spray_angle_rate)

        self.a.set_pin_mode_digital_output(self.spray_pin)

    def goto_point(self, point, prev_point=(0, 0)):
        """
        Move sprayer nozzle to point at a given point, in the form (x, y).
        Does not return until complete.

        point       the point to move to
        prev_point  the current position
        """

        # Calculate time to reach position by using maximum distance
        dist = max(abs(np.subtract(prev_point, point)))
        wait_time = dist * self.spray_dist2angle / self.spray_angle_rate

        # Move nozzle to position
        self.a.servo_write(self.servo_pin_x, int(
            point[0] * self.spray_dist2angle + 90))
        self.a.servo_write(self.servo_pin_y, int(
            point[1] * self.spray_dist2angle + 90))

        time.sleep(wait_time)

    def spray(self, enable=False):
        """
        Enable or disable the spray nozzle.
        """
        self.a.digital_pin_write(self.spray_pin, enable)

    def print_movement(self):
        """
        Prints the current movement position
        """
        log.info(json.loads(r.get(self.movement_key)))

    def correct_bbox(self, bbox):
        """
        Correct a list of bounding boxes according to the current movement.

        bbox        list of bounding boxes

        # return
        bbox        corrected list of bounding boxes
        """

        # Check and correct input values
        assert self.img_width == round(self.img_width)
        assert self.img_height == round(self.img_height)

        # For reference, current_movement is in the form:
        #   (x, y, a),
        # where +ve x, y is when the camera moves up, left respectively,
        #   and +ve a is when the camera moves anti-clockwise
        current_movement = json.loads(r.get(self.movement_key))

        for i in range(bbox['count']):
            box = bbox['bounding_boxes'][i]

            # Extract coordinates from input
            x, y = self.bbox2centre(box)
            w = box[2]
            h = box[3]

            # Convert centre points based on movement
            x = x + current_movement[0]
            y = y + current_movement[1]

            # Convert centre points based on rotation
            a = current_movement[2]
            rho, phi = self.cart2pol(x, y)
            x, y = self.pol2cart(rho, phi + a)

            # Convert w, h based on rotation
            w = w * np.cos(a) + h * np.sin(a)
            h = h * np.cos(a) + w * np.sin(a)

            # Convert back to bbox format
            new_bbox = self.centre2bbox(x, y, w, h)

            # Assign back into original dictionary
            bbox['bounding_boxes'][i] = new_bbox

        return bbox

    def correct_point(self, point):
        """
        Correct an (x, y) point according to the current movement.

        @return
        (x, y)      corrected (x, y) point
        """
        x = point[0]
        y = point[1]

        current_movement = json.loads(r.get(self.movement_key))

        # Convert centre points based on movement
        x = x + current_movement[0]
        y = y + current_movement[1]

        # Convert centre points based on rotation
        a = current_movement[2]
        rho, phi = self.cart2pol(x, y)
        x, y = self.pol2cart(rho, phi + a)

        return (x, y)

    def bbox2centre(self, bbox):
        """
        Converts a bounding box in the form (x, y, w, h) to a centre point (x, y),
        while also moving coordinates from bbox origin (top, left) to movement origin (centre).
        """

        # Convert to centre point
        x = bbox[0] + bbox[2] / 2
        y = bbox[1] + bbox[3] / 2

        # Convert to movement coordinates
        correct_x = self.img_width / 2
        correct_y = self.img_height / 2
        x = x - correct_x
        y = y - correct_y

        return (x, y)

    def centre2bbox(self, x, y, w, h):
        """
        Converts a centre point in the form (x, y) to a bounding box (x, y, w, h),
        while also moving coordinates from movement origin (centre), to bbox origin (top, left).
        """

        # Convert to bbox point
        x = x - w / 2
        y = y - h / 2

        # Convert to movement coordinates
        correct_x = self.img_width / 2
        correct_y = self.img_height / 2
        x = x + correct_x
        y = y + correct_y

        return (x, y, w, h)

    def cart2pol(self, x, y):
        """
        Convert from cartesian coordinates to polar coordinates.
        """
        rho = np.sqrt(x**2 + y**2)
        phi = np.arctan2(y, x)

        return(rho, phi)

    def pol2cart(self, rho, phi):
        """
        Convert from polar coordinates to cartesian coordinates.
        """
        x = rho * np.cos(phi)
        y = rho * np.sin(phi)

        return(x, y)
