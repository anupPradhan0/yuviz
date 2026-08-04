import sys
import os

# Make the generated proto stubs importable as "voiceai.v1.*" (absolute package).
_generated = os.path.join(os.path.dirname(__file__),
                          "services", "conversation", "generated")
if _generated not in sys.path:
    sys.path.insert(0, _generated)
