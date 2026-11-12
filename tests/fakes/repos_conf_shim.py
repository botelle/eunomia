"""Plan 0134: fleet-watch reads its repository list from config/repos.conf in its
own checkout, which for a test is the real fleet. Tests that predate that say
"this repo is listed" with FLEET_WATCH_REPOS, so after each load of
bin/fleet-watch this points its REPOS_CONF at a file built from that variable.
The variable is read HERE, by the test harness, never by fleet-watch. A test of
the real reader assigns `mod.REPOS_CONF` itself after loading and is left alone.
"""
import importlib.machinery
import os


def install(d):
    """Patch SourceFileLoader.exec_module; returns the original, to restore."""
    original = importlib.machinery.SourceFileLoader.exec_module

    def exec_module(self, module):
        original(self, module)
        if os.path.basename(str(self.path)) != "fleet-watch":
            return
        conf = d / f"repos-{len(list(d.iterdir()))}.conf"
        module.REPOS_CONF = conf
        real = module.conf_repos

        def conf_repos():
            # re-derived on every read: several tests set the variable after
            # loading.
            if module.REPOS_CONF == conf:
                names = [r.strip() for r in
                         os.environ.get("FLEET_WATCH_REPOS", "").split(",") if r.strip()]
                if names:
                    conf.write_text("".join(f"repo: {r} dispatch\n" for r in names))
                elif conf.exists():
                    conf.unlink()
            return real()
        module.conf_repos = conf_repos

    importlib.machinery.SourceFileLoader.exec_module = exec_module
    return original
