#!/usr/bin/env bash
# Point the `claude` CLI at the local translating proxy.
#
# Source this; do not run it. It is separate from the launcher so that a
# different launcher can reuse the environment discipline without inheriting
# the launcher's argument handling.
#
# The important line is the first export: an ANTHROPIC_BASE_URL left over in an
# operator's shell would silently redirect a campaign to a different endpoint,
# and the results would look entirely normal. Binding it unconditionally here
# means the run always talks to the proxy the driver started.

: "${RECURIS_PROXY_PORT:=4000}"
: "${RECURIS_META_MODEL:?RECURIS_META_MODEL must name the meta-agent's model}"

ANTHROPIC_BASE_URL="http://127.0.0.1:${RECURIS_PROXY_PORT}"
# Not a credential: the proxy accepts any token and injects the real key from
# its own environment. It exists because the CLI requires the variable to be
# set. Override with RECURIS_PROXY_TOKEN if a proxy that checks it is used.
ANTHROPIC_API_KEY="${RECURIS_PROXY_TOKEN:-local-proxy}"
ANTHROPIC_MODEL="${RECURIS_META_MODEL}"
ANTHROPIC_SMALL_FAST_MODEL="${RECURIS_META_MODEL}"

export RECURIS_PROXY_PORT RECURIS_META_MODEL
export ANTHROPIC_BASE_URL ANTHROPIC_API_KEY
export ANTHROPIC_MODEL ANTHROPIC_SMALL_FAST_MODEL

# The proxy is on loopback; never send it through an HTTP proxy.
export NO_PROXY=127.0.0.1,localhost
export no_proxy=127.0.0.1,localhost

# A campaign session is an experiment, not an interactive one.
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
export DISABLE_TELEMETRY=1
export DISABLE_AUTOUPDATER=1
export DISABLE_ERROR_REPORTING=1
export PYTHONUTF8=1

# Package evaluation takes minutes. At the default shell-tool timeout the call
# is backgrounded at 120 s, which hands the meta-agent an empty output file and
# no failure signal -- it then diagnoses from nothing and the round is wasted.
export BASH_DEFAULT_TIMEOUT_MS=600000
export BASH_MAX_TIMEOUT_MS=600000
