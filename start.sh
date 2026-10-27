#!/bin/sh

addgroup -g ${GID} madsonic && adduser -h /madsonic -s /bin/sh -D -G madsonic -u ${UID} madsonic

if [ ! -d /etc/app_configured ]
    then

    mkdir -p /config/transcode
    ln -s /usr/bin/ffmpeg /config/transcode/ffmpeg
    ln -s /usr/bin/lame /config/transcode/lame

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

    until nc -z localhost 80; do echo "waiting for madsonic"; sleep 2; done
    sleep 20

    echo "*****************************************************************************"
    echo "ADDING NEW ADMIN USER ${MAD_USERNAME}"
    echo "*****************************************************************************"
    # Add the new user
    curl -k "http://localhost/rest2/createUser.view?u=admin&p=admin&v=2.6.0&c=cylo&f=json&username=${MAD_USERNAME}&password=${MAD_PASSWORD}&email=${MAD_EMAIL}&adminRole=true"
    sleep 30

    # Break the admin login, no idea why this doesnt work!
    echo "*****************************************************************************"
    echo "DISABLING DEFAULT ADMIN LOGIN"
    echo "*****************************************************************************"
    curl -k "http://localhost/rest2/changePassword.view?u=${MAD_USERNAME}&p=${MAD_PASSWORD}&v=2.6.0&c=cylo&f=json&username=admin&password=${MAD_PASSWORD}"
    sleep 20

    echo "*****************************************************************************"
    echo "KILLING JAVA"
    echo "*****************************************************************************"
    kill -9 $(pgrep java)

    #Tell Apex we're done installing.
    curl -i -H "Accept: application/json" -H "Content-Type:application/json" -X POST "https://api.cylo.net/v1/apps/installed/$INSTANCE_ID"
    touch /etc/app_configured

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