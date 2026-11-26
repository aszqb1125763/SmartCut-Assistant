# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
import os
import tempfile
import unittest

import autocut


class SingleDecodeRendererTest(unittest.TestCase):
    def render_conf(self):
        conf = copy.deepcopy(autocut.DEFAULT_CONF["render"])
        conf.update({
            "encoder": "x264", "scale": "320:-2", "mix_tracks": True,
            "export_stems": True, "seam_tone_tail_db": 0.0,
        })
        return conf

    def command(self, pieces, folder, tracks=(0, 1)):
        source = os.path.join(folder, "source.mp4")
        output = os.path.join(folder, "output.mp4")
        graph = os.path.join(folder, "timeline_filter.txt")
        stems = [os.path.join(folder, "track_%d.wav" % track)
                 for track in tracks]
        command, duration = autocut._single_timeline_cmd(
            "ffmpeg", source, pieces, output, self.render_conf(), 30,
            "320:-2", tracks=list(tracks), output_size=(320, 180),
            stem_outputs=stems, filter_script_path=graph)
        return source, output, graph, stems, command, duration

    def test_default_hard_cut_uses_one_source_input(self):
        with tempfile.TemporaryDirectory() as folder:
            source, _output, graph, stems, command, duration = self.command(
                [("trim", 0.0, 1.0), ("trim", 2.0, 3.5)], folder)
            self.assertEqual(1, command.count(source))
            self.assertEqual(1, sum(
                command[index + 1] == source
                for index, value in enumerate(command[:-1]) if value == "-i"))
            self.assertIn("-filter_complex_script", command)
            self.assertNotIn("-filter_complex", command)
            self.assertEqual(2.5, duration)
            self.assertTrue(all(path in command for path in stems))
            with open(graph, encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("select=", text)
            self.assertIn("aselect=", text)

    def test_two_hundred_pieces_keep_graph_out_of_windows_argv(self):
        pieces = [("trim", index * 0.2, index * 0.2 + 0.1)
                  for index in range(200)]
        with tempfile.TemporaryDirectory() as folder:
            source, _output, graph, _stems, command, _duration = self.command(
                pieces, folder, tracks=(0,))
            self.assertEqual(1, command.count(source))
            self.assertLess(sum(len(value) + 1 for value in command), 4096)
            self.assertGreater(os.path.getsize(graph), 32767)

    def test_chapter_uses_local_silence_and_piece_concat(self):
        pieces = [
            ("trim", 0.0, 1.0, "out", 0.2, 0.1, 0.0, 0.0),
            ("chapter", 0.4),
            ("trim", 2.0, 3.0, "in", 0.2, 0.1, 0.0, 0.0),
        ]
        with tempfile.TemporaryDirectory() as folder:
            source, _output, graph, _stems, command, duration = self.command(
                pieces, folder)
            self.assertEqual(1, command.count(source))
            self.assertEqual(2.4, duration)
            with open(graph, encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("color=c=black", text)
            self.assertIn("anullsrc", text)
            self.assertIn("concat=n=3:v=1:a=0", text)
            self.assertIn("adelay=delays=48000S|48000S:all=1", text)
            self.assertIn("amix=inputs=3", text)
            self.assertNotIn("aselect=", text)

    def test_unmixed_chapter_emits_one_silent_stream_per_selected_track(self):
        conf = self.render_conf()
        conf["mix_tracks"] = False
        command, duration = autocut._piece_cmd(
            "ffmpeg", "source.mp4", ("chapter", 0.4), "chapter.mp4",
            conf, 30, "320:180", tracks=[1, 3])
        graph = command[command.index("-filter_complex") + 1]
        self.assertAlmostEqual(0.4, duration, places=6)
        self.assertIn("asplit=2[silent0][silent1]", graph)
        self.assertEqual(2, sum(
            1 for index, value in enumerate(command[:-1])
            if value == "-map" and command[index + 1] in ("[silent0]", "[silent1]")))

    def test_piece_concat_maps_video_and_every_selected_audio_stream(self):
        command = autocut._concat_piece_cmd(
            "ffmpeg", "concat.txt", "out.mp4")
        self.assertIn("0:v:0", command)
        self.assertIn("0:a?", command)

    def test_unverified_transitions_fail_closed_to_compatibility_renderer(self):
        for piece in (
            ("bridge", (1.0, 1.3), (2.0, 2.3), 0.2),
            ("audio_bridge", (1.0, 1.3), (2.0, 2.3), 0.2),
        ):
            pieces = [("trim", 0.0, 1.0), piece, ("trim", 3.2, 4.0)]
            eligible, reason = autocut._single_decode_eligibility(pieces)
            self.assertFalse(eligible)
            self.assertIn("未通过可听性验证", reason)
            with tempfile.TemporaryDirectory() as folder:
                source = os.path.join(folder, "source.mp4")
                with self.assertRaisesRegex(ValueError, "未通过可听性验证"):
                    autocut._single_timeline_cmd(
                        "ffmpeg", source, pieces,
                        os.path.join(folder, "output.mp4"),
                        self.render_conf(), 30, "320:-2", tracks=[0, 1],
                        output_size=(320, 180))

    def test_trim_and_chapter_remain_single_decode_eligible(self):
        pieces = [("trim", 0.0, 1.0), ("chapter", 0.4),
                  ("trim", 2.0, 3.0)]
        self.assertEqual((True, ""),
                         autocut._single_decode_eligibility(pieces))

    def test_audio_bridge_normalizes_each_crossfade_before_track_output(self):
        piece = ("audio_bridge", (1.0, 1.4), (2.0, 2.4), 0.2)
        expected = (
            "acrossfade=d=0.200:c1=qsin:c2=qsin,apad,atrim=0:0.400,"
            "aformat=sample_fmts=fltp:sample_rates=48000:"
            "channel_layouts=stereo")
        for mix_tracks in (False, True):
            with self.subTest(mix_tracks=mix_tracks):
                conf = self.render_conf()
                conf["mix_tracks"] = mix_tracks
                command, duration = autocut._piece_cmd(
                    "ffmpeg", "source.mp4", piece, "out.mp4", conf, 30,
                    "320:180", tracks=[0, 1])
                graph = command[command.index("-filter_complex") + 1]
                self.assertAlmostEqual(0.4, duration, places=6)
                self.assertEqual(2, graph.count(expected))
                if mix_tracks:
                    self.assertIn("amix=inputs=2", graph)
                else:
                    self.assertIn("-map", command)
                    self.assertIn("[a0]", graph)
                    self.assertIn("[a1]", graph)

    def test_free_clock_fails_closed_to_audited_compatibility_path(self):
        ok, reason = autocut._single_decode_eligibility(
            [("trim", 0.0, 1.0)], free_tracks=[1])
        self.assertFalse(ok)
        self.assertIn("自由音轨", reason)

    def test_global_fades_are_windowed_not_permanently_muting(self):
        rows, _duration = autocut._single_decode_rows([
            ("trim", 0.0, 1.0), ("trim", 2.0, 3.0)])
        audio = autocut._single_piece_effects(
            rows, {"seam_tone_tail_db": 0.0}, audio=True)
        self.assertTrue(any(value.startswith("volume=") for value in audio))
        self.assertFalse(any(value.startswith("afade=") for value in audio))


if __name__ == "__main__":
    unittest.main()
