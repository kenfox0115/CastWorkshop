import unittest
from scripts.recovery_report import completed_video

class RecoveryReportTests(unittest.TestCase):
    def entry(self, files, done=True):
        return {"status": {"completed": done, "status_str": "success"}, "outputs": {"92": {"images": files}}}

    def test_completed_video(self):
        video = {"filename": "clip.mp4", "type": "output"}
        self.assertEqual(completed_video(self.entry([video])), video)

    def test_scene_image_is_not_final_video(self):
        self.assertIsNone(completed_video(self.entry([{"filename": "scene.png", "type": "output"}])))

    def test_incomplete_or_ambiguous_is_not_recovered(self):
        video = {"filename": "clip.mp4", "type": "output"}
        self.assertIsNone(completed_video(self.entry([video], False)))
        self.assertIsNone(completed_video(self.entry([video, video])))
