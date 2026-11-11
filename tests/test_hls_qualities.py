import importlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote


class HlsQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="vela-hls-tests-", dir="/private/tmp")
        cls.root = Path(cls.temporary.name)
        with patch("pathlib.Path.home", return_value=cls.root):
            cls.api = importlib.import_module("api")

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_master_children_preserve_signed_urls_and_exclude_segments(self):
        manifest = '''#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="main",NAME="Main, stereo",URI="../audio/index.m3u8?token=audio"
#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=854x480
480p/index.m3u8?token=low
#EXT-X-STREAM-INF:BANDWIDTH=4500000,RESOLUTION=1920x1080
https://edge.example.com/1080p/index.m3u8?token=high&expires=123
'''
        self.assertEqual(self.api.hls_child_playlists(manifest, "https://cdn.example.com/video/master.m3u8"), [
            "https://cdn.example.com/audio/index.m3u8?token=audio",
            "https://cdn.example.com/video/480p/index.m3u8?token=low",
            "https://edge.example.com/1080p/index.m3u8?token=high&expires=123",
        ])
        self.assertEqual(self.api.hls_child_playlists("#EXTM3U\n#EXTINF:6,\nsegment.ts", "https://example.com/playlist"), [])

    def test_metadata_bypasses_old_single_resolution_cache(self):
        request = self.api.M3U8Request(url="https://example.com/playlist")
        with patch.object(self.api, "cached_media_info", return_value={}) as cache:
            self.api.get_m3u8_info(request)
        self.assertEqual(cache.call_args.args[0], "hls-qualities-v1")

    def test_fallback_keeps_the_selected_resolution(self):
        with patch.object(self.api, "run_ffmpeg_hls", side_effect=RuntimeError("test failure")), \
                patch.object(self.api, "run_ytdlp_hls_fallback", return_value="video.mp4") as fallback, \
                patch.object(self.api, "rename_stream_download", return_value="Episodio.mp4"), \
                patch.object(self.api, "complete_download") as complete:
            self.api.run_m3u8_download("https://example.com/playlist", "test-fallback", {}, "Episodio", 1, 720)
        self.assertEqual(fallback.call_args.args[-1], 720)
        complete.assert_called_once_with("test-fallback", filename="Episodio.mp4")

    def test_ytdlp_does_not_silently_fall_back_to_a_different_resolution(self):
        with patch.object(self.api.yt_dlp, "YoutubeDL") as factory:
            downloader = factory.return_value.__enter__.return_value
            downloader.extract_info.return_value = {"title": "Test"}
            downloader.prepare_filename.return_value = "video.mp4"
            self.api.run_ytdlp_hls_fallback("https://example.com/playlist", "test-options", {}, 720)
        self.assertEqual(factory.call_args.args[0]["format"], "best[height=720]/bestvideo[height=720]+bestaudio")

    def select_fallback_formats(self, url, height, formats):
        with self.api.yt_dlp.YoutubeDL({"quiet": True}) as downloader:
            selector = downloader.build_format_selector(self.api.hls_fallback_format(url, height))
            return downloader._select_formats(formats, selector)

    def test_fixed_renditions_accept_unknown_height_with_the_real_ytdlp_selector(self):
        source = "https://vixcloud.cc/playlist/64530?type=video&rendition=360p&token=test"
        unknown = {"format_id": "hls", "url": source, "ext": "mp4", "vcodec": "h264", "acodec": "aac"}
        # This is the metadata produced for media playlists without RESOLUTION.
        with self.api.yt_dlp.YoutubeDL({"quiet": True}) as downloader:
            old_selector = downloader.build_format_selector("best[height=360]/bestvideo[height=360]+bestaudio")
            self.assertEqual(downloader._select_formats([unknown], old_selector), [])
        proxy = f"https://ani3.api-webs.com/player.php?m3u8={quote(source, safe='')}"
        for url, height in [(source, 360), (proxy, 360), (source, None)]:
            with self.subTest(url=url, height=height):
                selected = self.select_fallback_formats(url, height, [unknown])
                self.assertEqual([item["format_id"] for item in selected], ["hls"])

    def test_fixed_renditions_still_reject_known_quality_mismatches(self):
        source = "https://vixcloud.cc/playlist/64530?type=video&rendition=360p"
        wrong = {"format_id": "720p", "url": source, "ext": "mp4", "height": 720, "vcodec": "h264", "acodec": "aac"}
        self.assertEqual(self.select_fallback_formats(source, 360, [wrong]), [])
        with self.assertRaisesRegex(RuntimeError, "360p.*720p"):
            self.api.hls_fallback_format(source, 720)

    def test_master_playlists_keep_strict_resolution_selection(self):
        source = "https://vixcloud.cc/playlist/64530?token=test&h=1"
        unknown = {"format_id": "unknown", "url": source, "ext": "mp4", "vcodec": "h264", "acodec": "aac"}
        self.assertEqual(self.select_fallback_formats(source, 360, [unknown]), [])
        known = [{**unknown, "format_id": "360p", "height": 360}, {**unknown, "format_id": "720p", "height": 720}]
        self.assertEqual([item["format_id"] for item in self.select_fallback_formats(source, 360, known)], ["360p"])

    def test_generic_io_errors_are_not_misreported_as_missing_segments(self):
        error = self.api.ffmpeg_download_error(1, [
            "[http] HTTP error 503 Service Unavailable\n",
            "[hls] Error during demuxing: Input/output error\n",
        ])
        self.assertIn("Errore di lettura", error)
        self.assertIn("503 Service Unavailable", error)
        self.assertNotIn("segmenti mancanti", error)
        self.assertIsNotNone(self.api.ffmpeg_download_error(0, ["Error during demuxing: Input/output error"]))

    def test_failed_download_logs_keep_the_complete_diagnostics_and_history_path(self):
        download_id = "full-diagnostics-test"
        self.api.create_download_entry(download_id, "https://example.com/index.m3u8", "Test", "hls")
        lines = [f"Diagnostic line {index}\n" for index in range(100)]
        try:
            path = self.api.save_ffmpeg_log(download_id, 1, iter(lines))
            self.assertIsNotNone(path)
            self.assertEqual(Path(path).read_text(), "FFmpeg exit code: 1\n\n" + "".join(lines))
            self.assertEqual(self.api.active_downloads[download_id]["diagnostic_log"], path)
        finally:
            with self.api.downloads_lock:
                self.api.active_downloads.pop(download_id, None)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
    def test_fixed_360p_fallback_downloads_a_real_media_playlist_without_resolution(self):
        fixture = Path(tempfile.mkdtemp(prefix="fixed-360p-", dir=self.root))
        playlist = fixture / "index.m3u8"
        subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=5",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "2", "-c:v", "libx264",
            "-preset", "ultrafast", "-c:a", "aac", "-f", "hls", "-hls_playlist_type", "vod", str(playlist),
        ], check=True, capture_output=True, timeout=20)
        playlist.write_text("\n".join(
            (fixture / line).as_uri() if line and not line.startswith("#") else line
            for line in playlist.read_text().splitlines()
        ) + "\n")
        logger = type("QuietLogger", (), {
            "debug": lambda *args: None, "info": lambda *args: None,
            "warning": lambda *args: None, "error": lambda *args: None,
        })()
        offline_options = {**self.api.YTDLP_JS_OPTIONS, "enable_file_urls": True, "logger": logger, "noprogress": True}
        with self.api.yt_dlp.YoutubeDL(offline_options) as downloader:
            info = downloader.extract_info(playlist.as_uri(), download=False)
        self.assertTrue(info["formats"])
        self.assertTrue(all(item.get("height") is None for item in info["formats"]))
        selector = self.api.hls_fallback_format("https://vixcloud.cc/playlist/64530?type=video&rendition=360p", 360)
        # File URLs cannot model the HTTP query; use the selector computed for the
        # real URL shape while keeping extraction and fragment downloads offline.
        with patch.object(self.api, "YTDLP_JS_OPTIONS", offline_options), \
                patch.object(self.api, "hls_fallback_format", return_value=selector):
            output = self.api.run_ytdlp_hls_fallback(playlist.as_uri(), "fixed-360p-test", height=360)
        result = subprocess.run([
            "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", output,
        ], check=True, capture_output=True, text=True, timeout=10)
        data = json.loads(result.stdout)
        videos = [stream for stream in data["streams"] if stream["codec_type"] == "video"]
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]["height"], 360)
        self.assertEqual(sum(stream["codec_type"] == "audio" for stream in data["streams"]), 1)
        self.assertIn("mp4", data["format"]["format_name"])

    def test_unwraps_html_players_without_leaking_host_credentials(self):
        embedded = "https://vixcloud.cc/playlist/793409?token=abc%2Bdef&expires=123&h=1"
        wrapper = f"https://ani3.api-webs.com/player.php?m3u8={quote(embedded, safe='')}"
        headers = {"Cookie": "player-session", "Authorization": "Bearer player", "User-Agent": "Test"}
        with patch.object(self.api, "urlopen") as request:
            request.return_value.__enter__.return_value.read.return_value = b"<!doctype html><html>player</html>"
            url, actual_headers = self.api.resolve_hls_source(wrapper, headers)
        self.assertEqual(url, embedded)
        self.assertNotIn("Cookie", actual_headers)
        self.assertNotIn("Authorization", actual_headers)
        self.assertEqual(actual_headers["Referer"], wrapper)
        self.assertEqual(actual_headers["Origin"], "https://ani3.api-webs.com")
        self.assertEqual(actual_headers["User-Agent"], "Test")

    def test_preserves_a_proxy_that_returns_an_actual_hls_playlist(self):
        wrapper = f"https://ani3.api-webs.com/player.php?m3u8={quote('https://vixcloud.cc/playlist/793409', safe='')}"
        headers = {"Cookie": "proxy-session"}
        with patch.object(self.api, "urlopen") as request:
            request.return_value.__enter__.return_value.read.return_value = b"#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
            self.assertEqual(self.api.resolve_hls_source(wrapper, headers), (wrapper, headers))

    def test_direct_playlists_need_no_extra_resolution_request(self):
        url = "https://vixcloud.cc/playlist/793409?token=test&h=1"
        with patch.object(self.api, "urlopen") as request:
            self.assertEqual(self.api.resolve_hls_source(url, {}), (url, {}))
        request.assert_not_called()

    def test_probe_returns_the_actual_decoder_error(self):
        error = subprocess.CalledProcessError(1, ["ffprobe"], stderr="Proxy segment format rejected")
        with patch.object(self.api.subprocess, "run", side_effect=error):
            result = self.api.probe_stream("https://example.com/player.php?m3u8=https%3A%2F%2Fexample.com%2Fplaylist")
        self.assertEqual(result["error"], "Proxy segment format rejected")

    def test_failed_analysis_does_not_repeat_the_request_for_a_thumbnail(self):
        with patch.object(self.api, "probe_stream", return_value={"error": "Accesso negato"}), \
                patch.object(self.api, "create_stream_thumbnail") as thumbnail:
            result = self.api.get_m3u8_info(self.api.M3U8Request(url="https://example.com/failed.m3u8"))
        self.assertEqual(result["error"], "Accesso negato")
        thumbnail.assert_not_called()

    def test_skipped_segments_are_rejected_even_when_ffmpeg_returns_success(self):
        error = self.api.ffmpeg_download_error(0, [
            "[hls] Failed to open segment 12 of playlist 0\n",
            "[hls] Segment 12 of playlist 0 failed too many times, skipping\n",
        ])
        self.assertIn("Download incompleto", error)
        self.assertIn("Segment 12", error)

    def test_recovered_network_errors_do_not_mark_a_complete_download_as_corrupt(self):
        self.assertIsNone(self.api.ffmpeg_download_error(0, [
            "[http] HTTP error 503 Service Unavailable\n",
            "[hls] Failed to open segment 12 of playlist 0\n",
            "[http] Will reconnect in 1 second\n",
        ]))

    def test_failed_primary_and_fallback_downloads_are_not_marked_completed(self):
        with patch.object(self.api, "run_ffmpeg_hls", side_effect=RuntimeError("Segmento mancante")), \
                patch.object(self.api, "run_ytdlp_hls_fallback", side_effect=RuntimeError("Fragment not found")), \
                patch.object(self.api, "rename_stream_download") as rename, \
                patch.object(self.api, "complete_download") as complete:
            self.api.run_m3u8_download("https://example.com/index.m3u8", "missing-fragment", {}, "Episodio")
        complete.assert_called_once()
        self.assertIn("Fragment not found", str(complete.call_args.kwargs["error"]))
        self.assertNotIn("filename", complete.call_args.kwargs)
        rename.assert_not_called()

    def missing_segment_fixture(self):
        fixture = Path(tempfile.mkdtemp(prefix="segment-loss-", dir=self.root))
        playlist = fixture / "index.m3u8"
        subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=5",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "8", "-c:v", "libx264",
            "-preset", "ultrafast", "-g", "5", "-sc_threshold", "0", "-c:a", "aac",
            "-f", "hls", "-hls_time", "1", "-hls_playlist_type", "vod", str(playlist),
        ], check=True, capture_output=True, timeout=20)
        segments = sorted(fixture.glob("*.ts"))
        self.assertGreaterEqual(len(segments), 5)
        segments[len(segments) // 2].unlink()
        return playlist

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
    def test_real_ffmpeg_download_rejects_a_missing_middle_segment(self):
        playlist = self.missing_segment_fixture()
        with self.assertRaisesRegex(RuntimeError, "Download incompleto|corrupt") as failure:
            self.api.run_ffmpeg_hls(str(playlist), "ffmpeg-missing-segment")
        path = self.api.CACHE_DIR / "logs" / "ffmpeg-ffmpeg-missing-segment.log"
        self.assertTrue(path.exists())
        self.assertIn(str(path), str(failure.exception))
        self.assertIn("skipping", path.read_text())

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
    def test_native_fallback_aborts_when_a_middle_fragment_is_missing(self):
        playlist = self.missing_segment_fixture()
        lines = playlist.read_text().splitlines()
        missing = next(line for line in lines if line and not line.startswith("#") and not (playlist.parent / line).exists())
        # yt-dlp only joins relative HLS URLs for HTTP. Explicit file URLs keep
        # this integration test entirely offline while exercising real fragments.
        playlist.write_text("\n".join(
            (playlist.parent / line).as_uri() if line and not line.startswith("#") else line
            for line in lines
        ) + "\n")
        logger = type("QuietLogger", (), {
            "debug": lambda *args: None, "info": lambda *args: None,
            "warning": lambda *args: None, "error": lambda *args: None,
        })()
        # Enable file URLs only in this offline fixture; production keeps them disabled.
        with patch.object(self.api, "YTDLP_JS_OPTIONS", {
            **self.api.YTDLP_JS_OPTIONS, "enable_file_urls": True, "logger": logger,
            "noprogress": True,
        }), patch.object(self.api, "hls_retry_delay", return_value=0):
            with self.assertRaises(self.api.yt_dlp.utils.DownloadError) as failure:
                self.api.run_ytdlp_hls_fallback(playlist.as_uri(), "native-missing-segment")
        self.assertIn(missing, str(failure.exception))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
    def test_php_playlists_and_segments_work_for_analysis_preview_and_download(self):
        fixture = self.root / "php-fixture"
        fixture.mkdir()
        for width, height in [(854, 480), (1920, 1080)]:
            subprocess.run([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r=2",
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "2", "-c:v", "libx264",
                "-preset", "ultrafast", "-c:a", "aac", "-f", "hls", "-hls_playlist_type", "vod",
                "-hls_segment_filename", str(fixture / f"segment-{height}-%03d.php"), str(fixture / f"{height}p.php"),
            ], check=True, capture_output=True, timeout=20)
        master = fixture / "master.php"
        master.write_text('''#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=854x480
480p.php
#EXT-X-STREAM-INF:BANDWIDTH=4500000,RESOLUTION=1920x1080
1080p.php
''')
        info = self.api.probe_stream(str(master))
        self.assertNotIn("error", info)
        self.assertEqual([quality["height"] for quality in info["qualities"]], [1080, 480])
        thumbnail = self.api.create_stream_thumbnail(str(master))
        self.assertIsNotNone(thumbnail)
        self.assertTrue(thumbnail.startswith("data:image/jpeg;base64,"))
        output = self.api.run_ffmpeg_hls(str(master), "php-proxy-test", program_id=info["qualities"][0]["program_id"])
        result = subprocess.run([
            "ffprobe", "-v", "error", "-show_streams", "-of", "json", output,
        ], check=True, capture_output=True, text=True, timeout=10)
        streams = json.loads(result.stdout)["streams"]
        videos = [stream for stream in streams if stream["codec_type"] == "video"]
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]["height"], 1080)
        self.assertEqual(sum(stream["codec_type"] == "audio" for stream in streams), 1)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
    def test_real_master_selects_one_video_and_its_external_audio(self):
        fixture = self.root / "fixture"
        fixture.mkdir()
        for width, height in [(854, 480), (1280, 720), (1920, 1080)]:
            subprocess.run([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r=2",
                "-t", "2", "-c:v", "libx264", "-preset", "ultrafast", "-f", "hls", "-hls_time", "1",
                "-hls_playlist_type", "vod", str(fixture / f"{height}p.m3u8"),
            ], check=True, capture_output=True, timeout=20)
        subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "2",
            "-c:a", "aac", "-f", "hls", "-hls_playlist_type", "vod", str(fixture / "audio.m3u8"),
        ], check=True, capture_output=True, timeout=20)
        master = fixture / "master.m3u8"
        master.write_text('''#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Main",DEFAULT=YES,URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=854x480,AUDIO="audio"
480p.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1800000,RESOLUTION=1280x720,AUDIO="audio"
720p.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=4500000,RESOLUTION=1920x1080,AUDIO="audio"
1080p.m3u8
''')
        info = self.api.probe_stream(str(master))
        self.assertNotIn("error", info)
        self.assertEqual(info["resolution"], "1920x1080")
        self.assertTrue(info["has_video"])
        self.assertTrue(info["has_audio"])
        self.assertEqual([quality["height"] for quality in info["qualities"]], [1080, 720, 480])
        medium = next(quality for quality in info["qualities"] if quality["height"] == 720)
        for height, program_id in [(1080, None), (720, medium["program_id"])]:
            output = self.api.run_ffmpeg_hls(str(master), f"test{height}", program_id=program_id)
            result = subprocess.run([
                "ffprobe", "-v", "error", "-show_streams", "-of", "json", output,
            ], check=True, capture_output=True, text=True, timeout=10)
            streams = json.loads(result.stdout)["streams"]
            video = [stream for stream in streams if stream["codec_type"] == "video"]
            audio = [stream for stream in streams if stream["codec_type"] == "audio"]
            self.assertEqual(len(video), 1)
            self.assertEqual(video[0]["height"], height)
            self.assertEqual(len(audio), 1)


if __name__ == "__main__":
    unittest.main()
