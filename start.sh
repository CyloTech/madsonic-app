#!/bin/sh
# Do not add `set -x`: the trace would copy MAD_PASSWORD and the callback
# token in INSTANCE_ID into the container log.

if ! id madsonic >/dev/null 2>&1; then
    addgroup -g ${GID} madsonic && adduser -h /madsonic -s /bin/sh -D -G madsonic -u ${UID} madsonic
fi

# /config is the persistent volume. configured_marker records that this
# installation's users were set up, so restarts, updates and re-created
# containers never run the setup again. install_pending is present from the
# first start of a new installation until its setup has been verified.
# /etc/app_configured only records that this container has queued its
# installed callback.
configured_marker=/config/.appbox_configured
install_pending=/config/.appbox_install_pending
callback_pending=/etc/appbox_callback_pending

# Hex-encode $1. Madsonic decodes "enc:<hex>" passwords, so every character
# of a password reaches it unchanged.
hex() {
    printf '%s' "$1" | od -An -v -tx1 | tr -d ' \n'
}

# madsonic_api VIEW USER PASSWORD [curl options...]: call /rest2/VIEW.view
# authenticated as USER and print the JSON response. Every parameter is
# URL-encoded.
madsonic_api() {
    api_view=$1
    api_user=$2
    api_password=$3
    shift 3
    curl -G -s --max-time 30 \
        --data-urlencode "u=${api_user}" \
        --data-urlencode "p=enc:$(hex "${api_password}")" \
        --data-urlencode "v=2.6.0" \
        --data-urlencode "c=cylo" \
        --data-urlencode "f=json" \
        "$@" "http://127.0.0.1/rest2/${api_view}.view"
}

# Print "ok", or "failed <error code>", from a Madsonic API response on stdin.
api_status() {
    api_response=$(tr -d ' \n')
    api_result=$(printf '%s' "${api_response}" | grep -o '"status":"[a-z]*"' | head -1 | cut -d'"' -f4)
    api_code=$(printf '%s' "${api_response}" | grep -o '"code":[0-9]*' | head -1 | cut -d: -f2)
    echo "${api_result:-no-response}${api_code:+ ${api_code}}"
}

# Wait up to 30 minutes for the Madsonic API to answer. The first start
# unpacks the web app and creates the database, which is slow on a busy disk.
wait_for_api() {
    wait_count=0
    while [ "${wait_count}" -lt 900 ]; do
        madsonic_api ping admin admin | grep -q '"madsonic-response"' && return 0
        wait_count=$((wait_count + 1))
        sleep 2
    done
    return 1
}

# Stop the Java process started for the setup, giving it a minute to shut
# its database down cleanly.
stop_madsonic() {
    stop_count=0
    pkill java
    while pgrep java >/dev/null && [ "${stop_count}" -lt 60 ]; do
        stop_count=$((stop_count + 1))
        sleep 1
    done
    pkill -9 java
    return 0
}

fail_install() {
    echo "Madsonic setup failed: $1. The installation was not confirmed to Appbox." >&2
    stop_madsonic
    exit 1
}

# POST the installed callback. Network errors, 408, 429 and 5xx are retried
# with backoff; any other status is final. Only the numeric instance ID is
# logged.
send_installed_callback() {
    if [ -z "${INSTANCE_ID:-}" ]; then
        echo "INSTANCE_ID is not set; no installed callback to send"
        return 0
    fi
    callback_url="${APPBOX_CALLBACK_BASE:-https://api.cylo.net}/v1/apps/installed/${INSTANCE_ID}"
    callback_label="${INSTANCE_ID%%\?*}"
    callback_delay=5
    set -- -s -o /dev/null -w '%{http_code}' --connect-timeout 10 --max-time 30 \
        -H "Accept: application/json" -H "Content-Type: application/json" -X POST
    if [ -n "${CALLBACK_TOKEN:-}" ]; then
        set -- "$@" -H "X-Cylo-Callback-Token: ${CALLBACK_TOKEN}"
    fi
    while :; do
        callback_status=$(curl "$@" "${callback_url}" 2>/dev/null) || callback_status=000
        case "${callback_status}" in
            2??)
                echo "Instance ${callback_label}: installed callback accepted (HTTP ${callback_status})"
                return 0
                ;;
            000|408|429|5??)
                echo "Instance ${callback_label}: installed callback got HTTP ${callback_status}, retrying in ${callback_delay}s"
                sleep "${callback_delay}"
                callback_delay=$((callback_delay * 2))
                [ "${callback_delay}" -gt 60 ] && callback_delay=60
                ;;
            *)
                echo "Instance ${callback_label}: installed callback rejected (HTTP ${callback_status}), not retrying"
                return 1
                ;;
        esac
    done
}

if [ -f "${configured_marker}" ]; then
    echo "Madsonic is already set up."
elif [ ! -f "${install_pending}" ] && grep -q "^INSERT INTO USER VALUES('" /config/db/madsonic.script 2>/dev/null; then
    # Installed by an earlier image, which kept its marker in /etc.
    echo "Existing Madsonic database found; keeping its users."
    touch "${configured_marker}"
else
    if [ -z "${MAD_USERNAME}" ] || [ -z "${MAD_PASSWORD}" ]; then
        echo "Madsonic setup failed: MAD_USERNAME and MAD_PASSWORD must be set for a new installation." >&2
        exit 1
    fi
    touch "${install_pending}"

    mkdir -p /config/transcode
    ln -sf /usr/bin/ffmpeg /config/transcode/ffmpeg
    ln -sf /usr/bin/lame /config/transcode/lame

    chown -R madsonic:madsonic /config

    sleep 10 # avoid erros

    echo "*****************************************************************************"
    echo "STARTING JAVA"
    echo "*****************************************************************************"

su madsonic << EOF
java -Xmx${JVM_MEMORY}m \
  -Dmadsonic.home=/config \
  -Dmadsonic.host=0.0.0.0 \
  -Dmadsonic.port=80 \
  -Dmadsonic.httpsPort=0 \
  -Dmadsonic.contextPath=/ \
  -Dmadsonic.defaultMusicFolder=/media \
  -Dmadsonic.defaultPodcastFolder=/podcasts \
  -Dmadsonic.defaultPlaylistFolder=/playlists \
  -Djava.awt.headless=true \
  -jar madsonic-booter.jar &
EOF

    echo "waiting for madsonic"
    wait_for_api || fail_install "Madsonic did not start"

    echo "*****************************************************************************"
    echo "ADDING NEW ADMIN USER ${MAD_USERNAME}"
    echo "*****************************************************************************"
    echo "createUser: $(madsonic_api createUser admin admin \
        --data-urlencode "username=${MAD_USERNAME}" \
        --data-urlencode "password=enc:$(hex "${MAD_PASSWORD}")" \
        --data-urlencode "email=${MAD_EMAIL}" \
        --data-urlencode "adminRole=true" | api_status)"

    # The built-in admin account gets a random password that nobody knows.
    echo "*****************************************************************************"
    echo "DISABLING DEFAULT ADMIN LOGIN"
    echo "*****************************************************************************"
    admin_password=$(head -c 24 /dev/urandom | od -An -v -tx1 | tr -d ' \n')
    echo "changePassword: $(madsonic_api changePassword "${MAD_USERNAME}" "${MAD_PASSWORD}" \
        --data-urlencode "username=admin" \
        --data-urlencode "password=enc:$(hex "${admin_password}")" | api_status)"
    admin_password=

    # Confirm the result before telling Appbox the app is installed: admin:admin
    # must be refused (error 40) and the new account must log in as an admin.
    admin_check=$(madsonic_api ping admin admin | api_status)
    [ "${admin_check}" = "failed 40" ] || fail_install "the default admin login is still accepted (${admin_check})"
    user_check=$(madsonic_api ping "${MAD_USERNAME}" "${MAD_PASSWORD}" | api_status)
    [ "${user_check}" = "ok" ] || fail_install "the new account cannot log in (${user_check})"
    madsonic_api getUser "${MAD_USERNAME}" "${MAD_PASSWORD}" --data-urlencode "username=${MAD_USERNAME}" \
        | tr -d ' \n' | grep -q '"adminRole":true' || fail_install "the new account is not an administrator"
    echo "Madsonic setup verified: the new account works and admin:admin is refused."

    echo "*****************************************************************************"
    echo "STOPPING JAVA"
    echo "*****************************************************************************"
    stop_madsonic

    touch "${configured_marker}"
    rm -f "${install_pending}"
fi

# Tell Appbox this container is installed, once per container. The flag is
# set before /etc/app_configured, so a restart in between cannot lose the
# callback, and the callback runs in the background.
if [ ! -f /etc/app_configured ]; then
    touch "${callback_pending}"
    touch /etc/app_configured
fi
if [ -f "${callback_pending}" ]; then
    (
        send_installed_callback
        rm -f "${callback_pending}"
    ) </dev/null &
fi

echo "*****************************************************************************"
echo "STARTING JAVA"
echo "*****************************************************************************"
su madsonic << EOF
java -Xmx${JVM_MEMORY}m \
  -Dmadsonic.home=/config \
  -Dmadsonic.host=0.0.0.0 \
  -Dmadsonic.port=80 \
  -Dmadsonic.httpsPort=0 \
  -Dmadsonic.contextPath=/ \
  -Dmadsonic.defaultMusicFolder=/media \
  -Dmadsonic.defaultPodcastFolder=/podcasts \
  -Dmadsonic.defaultPlaylistFolder=/playlists \
  -Djava.awt.headless=true \
  -jar madsonic-booter.jar
EOF
