# mod_audio_fork

#### [mod_audio_fork](src/README.md)
A Freeswitch module that attaches a bug to a media server endpoint and streams L16 audio via websockets to a remote server. The audio is never stored to disk locally on the media server, making it ideal for "no data at rest" type of applications.  This module also supports receiving media from the server to play back to the caller, enabling the creation of full-fledged IVR or dialog-type applications.
