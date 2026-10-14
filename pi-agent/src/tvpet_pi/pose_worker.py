"""Private stdin/stdout inference process. Loaded only in the isolated pose environment."""

import importlib
import json
import os
import struct
import sys
import time

from .pose import PoseTracker


def main() -> None:
    os.nice(10)
    cv2 = importlib.import_module("cv2")
    np = importlib.import_module("numpy")
    mp = importlib.import_module("mediapipe")
    cv2.setNumThreads(1)
    options = mp.tasks.vision.PoseLandmarkerOptions(
        base_options=mp.tasks.BaseOptions(model_asset_path=sys.argv[1]),
        running_mode=mp.tasks.vision.RunningMode.VIDEO,
        num_poses=3,
        min_pose_detection_confidence=0.6,
        min_pose_presence_confidence=0.6,
        min_tracking_confidence=0.6,
        output_segmentation_masks=False,
    )
    tracker = PoseTracker()
    with mp.tasks.vision.PoseLandmarker.create_from_options(options) as detector:
        print(json.dumps({"ready": True}), flush=True)
        last_ms = 0
        while header := sys.stdin.buffer.read(4):
            if len(header) != 4:
                return
            length = struct.unpack("!I", header)[0]
            if not 0 < length <= 1_048_576:
                return
            payload = sys.stdin.buffer.read(length)
            if len(payload) != length:
                return
            start = time.monotonic()
            image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None or image.shape[:2] != (480, 640):
                raise ValueError("invalid camera image")
            last_ms = max(last_ms + 1, int(start * 1000))
            result = detector.detect_for_video(
                mp.Image(
                    image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
                ),
                last_ms,
            )
            people = [
                [
                    [round(p.x, 4), round(p.y, 4), round(p.z, 4), round(p.visibility, 3)]
                    for p in landmarks
                ]
                for landmarks in result.pose_landmarks
            ]
            output = {
                "state": "active",
                "model": "mediapipe-pose-lite-v1",
                "rules": "geometry-v2",
                "people": tracker.update(people, start),
                "inference_ms": round((time.monotonic() - start) * 1000, 1),
            }
            print(json.dumps(output, ensure_ascii=False, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
