"""Put this directory on PYTHONPATH of a subprocess that loads bin/fleet-watch
(bin/mopsus does) and it reads FLEET_WATCH_REPOS through repos_conf_shim."""
import pathlib
import tempfile

import repos_conf_shim

repos_conf_shim.install(pathlib.Path(tempfile.mkdtemp(prefix="repos-conf-")))
