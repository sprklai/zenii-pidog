#!/usr/bin/env python3
"""Live camera stream from PiDog nose camera using SunFounder Vilib.

Vilib is already installed as part of the SunFounder PiDog ecosystem.
It starts an MJPEG server on port 9000 automatically.

Usage:
  sudo python3 camera_stream.py
  sudo python3 camera_stream.py --vflip    # flip vertically if image is upside down
  sudo python3 camera_stream.py --hflip    # flip horizontally

View stream:
  Browser : http://<pi-ip>:9000/mjpg
  VLC     : Open Network Stream → http://<pi-ip>:9000/mjpg
"""

import argparse
import time

from vilib import Vilib


def main() -> None:
    ap = argparse.ArgumentParser(description="PiDog camera MJPEG stream")
    ap.add_argument("--vflip", action="store_true", help="Flip image vertically")
    ap.add_argument("--hflip", action="store_true", help="Flip image horizontally")
    args = ap.parse_args()

    Vilib.camera_start(vflip=args.vflip, hflip=args.hflip)
    Vilib.display(local=False, web=True)

    print("=" * 40)
    print("  PiDog Camera Stream")
    print("  http://<pi-ip>:9000/mjpg")
    print("  Ctrl-C to stop")
    print("=" * 40)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        Vilib.camera_close()
        print("Camera stopped.")


if __name__ == "__main__":
    main()
