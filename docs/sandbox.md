# Sandbox

A Linux computer in a container, for trying the agent on a real screen without it touching
yours. It has a virtual display, a window manager and Chromium, and you watch it in a browser
tab. Your working copy is mounted in, so an edit applies to the next run without a rebuild, and
run folders land in `./runs` as usual. It reads keys from `.env`.

```
scripts/sandbox up        # build if needed and start; prints the URL to watch it
scripts/sandbox run clicker-bench loop --fixture --headed --goal "open the Q3 quarterly report"
scripts/sandbox reset     # close every app, clear browser profiles: a clean desktop
scripts/sandbox shell     # a shell inside, as the agent user
scripts/sandbox down      # stop and remove it; `destroy` also removes the image and volumes
```

One sandbox stays up across experiments, so a run starts in under a second; `reset` gives
each experiment a clean desktop. Only the browser backend runs there so far: the desktop loop
(`clicker`) still needs its macOS adapter. Chromium runs with `--no-sandbox`, since the
container is the boundary. Watch it at http://localhost:6080/vnc.html?autoconnect=1&resize=scale,
reachable from this machine only.
