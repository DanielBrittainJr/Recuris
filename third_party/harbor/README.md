# Harbor

Upstream: <https://github.com/harbor-framework/harbor>, version 0.20.0,
Apache-2.0. Harbor is the runner for both SkillFlow and Terminal-Bench 2.1.
It is a normal dependency: install it the usual way.

`harbor-0.20.0-docker.patch` is **conditional**. On a normal Docker host, stock
harbor runs both benchmarks and you should not apply it. Apply it only if you
hit one of these:

**Exec returns before the container process finishes.** The verifier then reads
the reward file before the task has written it, and a passing task is recorded
as a failure. The patch keeps the exec stdin pipe open for the lifetime of the
call, and runs `compose exec` with `-T` (no TTY allocation). We saw this on a
rootless Docker setup.

**`docker compose cp` is unavailable or refused.** Some hardened socket proxies
do not expose it. Setting `HARBOR_DOCKER_UPLOAD_MODE=tar` makes uploads use the
tar path unconditionally instead of trying `cp` first and falling back.

```bash
bash third_party/harbor/apply.sh /path/to/harbor
```

The script refuses a wheel install (there is nothing to patch), warns if the
version is not 0.20.0, and is idempotent: a second run detects that the patch
is already applied and exits cleanly.

Per Apache-2.0 §4, files modified by this patch carry a stated change: the two
files under `src/harbor/environments/docker/` are modified as described above.
The attribution notice is in `../../THIRD_PARTY_NOTICES.md`.
