# Madsonic 6.3.9540 on Alpine with OpenJDK 8 and ffmpeg.
#
# The original recipe (see this file's git history) can no longer be rebuilt:
# it installs from alpine:edge, which has moved on, and madsonic.org, which no
# longer answers. Releases therefore start from the published image that
# catalogue version 6.3-2 runs and replace only the Appbox start-up script.
FROM repo.cylo.net/madsonic@sha256:c26e81397beebc41b67937d65ae232d4771e7ce04b7debdac585477f63f66fdc

COPY start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh
