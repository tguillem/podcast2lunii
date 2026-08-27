# Third-party components

Python runtime dependencies are pinned in [requirements.txt](requirements.txt).

`ffmpeg` and `ffprobe` are required at runtime and are not bundled; install
them through your system package manager.

The Piper voice downloaded by `tools/bootstrap.py --with-voice` is pinned by
revision and SHA-256 in [tools/dependencies.json](tools/dependencies.json).
It comes from the MIT-licensed
[rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices) repository
and is trained on the [SIWIS](https://datashare.ed.ac.uk/handle/10283/2353)
corpus, licensed CC-BY-4.0.

See [NOTICE.md](NOTICE.md) for adapted STUdio source.
