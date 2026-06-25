"""tscribe-class-carnyx: a thin HTTP wrapper around the verified class pipeline.

The transcription/verification logic lives in `transcription_tool.class_pipeline`
(the tscribe package). This package only adds the job lifecycle and the HTTP
surface so a cloud agent can drive carnyx over a Cloudflare Tunnel.
"""

__version__ = "0.1.0"
