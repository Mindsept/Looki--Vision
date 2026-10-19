"""Class-separated ByteTrack instances. No predicted object is emitted as a detection."""

from types import SimpleNamespace

import numpy as np


class ByteTrackAdapter:
    def __init__(self):
        self.trackers = {}

    def update(self, detections):
        from ultralytics.engine.results import Boxes
        from ultralytics.trackers.byte_tracker import BYTETracker

        assignments = {}
        labels = {d.label for d in detections} | self.trackers.keys()
        for label in sorted(labels):
            if label not in self.trackers:
                self.trackers[label] = BYTETracker(
                    SimpleNamespace(
                        track_high_thresh=0.5,
                        track_low_thresh=0.1,
                        new_track_thresh=0.5,
                        track_buffer=15,
                        match_thresh=0.8,
                        fuse_score=True,
                    )
                )
            indices = [i for i, d in enumerate(detections) if d.label == label]
            rows = np.asarray(
                [
                    [*(v * 1000 for v in detections[i].bbox), detections[i].confidence, 0]
                    for i in indices
                ],
                dtype=np.float32,
            ).reshape(-1, 6)
            tracks = self.trackers[label].update(Boxes(rows, (1000, 1000)))
            for track in tracks:
                index = int(track[-1])
                if 0 <= index < len(indices):
                    assignments[indices[index]] = f"{label}:{int(track[4])}"
        return assignments
