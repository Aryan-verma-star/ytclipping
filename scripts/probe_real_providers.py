"""Live probe of the REAL downloader providers from this environment.

Documents actual behavior for the phase report (spec §10: report only what
was actually run). Expected and verified: both fail from a datacenter IP —
cobalt official API requires JWT auth; yt-dlp hits YouTube's bot check.
"""

import sys
import time

sys.path.insert(0, "/home/z/my-project/backend")

from app.config import Settings
from app.downloader.base import ProviderError
from app.downloader.cobalt import CobaltProvider
from app.downloader.ytdlp import YtDlpProvider

settings = Settings()
settings.ensure_dirs()

URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # "Me at the zoo", 19s

print("=== cobalt (official public instance, no key configured) ===")
try:
    CobaltProvider(settings).get_video(URL, 0, 5)
    print("UNEXPECTED SUCCESS")
except ProviderError as exc:
    print(f"ProviderError: {exc.message[:200]}")

print("\n=== yt-dlp (direct from this datacenter IP, no cookies) ===")
t0 = time.time()
try:
    YtDlpProvider(settings).get_video(URL, 0, 5)
    print("UNEXPECTED SUCCESS")
except ProviderError as exc:
    print(f"ProviderError ({time.time() - t0:.1f}s): {exc.message[:300]}")
