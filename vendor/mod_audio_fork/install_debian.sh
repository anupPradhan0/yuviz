#!/bin/bash

FREESWITCH_VERSION=v1.10.9
LWS_VERSION=v3.2.3
DIR=$PWD

git clone https://gitee.com/gridsoft/freeswitch.git -b ${FREESWITCH_VERSION}  /usr/src/freeswitch
git clone https://github.com/signalwire/libks /usr/src/libs/libks
git clone https://github.com/freeswitch/sofia-sip /usr/src/libs/sofia-sip
git clone https://github.com/freeswitch/spandsp /usr/src/libs/spandsp
git clone https://github.com/signalwire/signalwire-c /usr/src/libs/signalwire-c
git clone https://github.com/warmcat/libwebsockets.git -b ${LWS_VERSION}  /usr/src/libs/libwebsockets
git clone https://gitee.com/gridsoft/mod_audio_fork.git /usr/src/libs/mod_audio_fork

DEBIAN_FRONTEND=noninteractive apt-get -yq install build-essential cmake automake autoconf 'libtool-bin|libtool' pkg-config \
    libssl-dev zlib1g-dev libdb-dev unixodbc-dev libncurses5-dev libexpat1-dev libgdbm-dev bison erlang-dev libtpl-dev libtiff5-dev uuid-dev \
    libpcre3-dev libedit-dev libsqlite3-dev libcurl4-openssl-dev nasm \
    libogg-dev libspeex-dev libspeexdsp-dev \
    libldns-dev \
    python3-dev \
    libavformat-dev libswscale-dev libavresample-dev \
    liblua5.2-dev \
    libopus-dev \
    libpq-dev \
    libsndfile1-dev libflac-dev libogg-dev libvorbis-dev \
    libshout3-dev libmpg123-dev libmp3lame-dev

cd /usr/src/libs/libks && cmake . -DCMAKE_INSTALL_PREFIX=/usr -DWITH_LIBBACKTRACE=1 && make install
cd /usr/src/libs/sofia-sip && ./bootstrap.sh && ./configure CFLAGS="-g -ggdb" --with-pic --with-glib=no --without-doxygen --disable-stun --prefix=/usr && make -j`nproc --all` && make install
cd /usr/src/libs/spandsp && ./bootstrap.sh && ./configure CFLAGS="-g -ggdb" --with-pic --prefix=/usr && make -j`nproc --all` && make install
cd /usr/src/libs/signalwire-c && PKG_CONFIG_PATH=/usr/lib/pkgconfig cmake . -DCMAKE_INSTALL_PREFIX=/usr && make install

# build libwebsockets
cd /usr/src/libs/libwebsockets
mkdir -p build && cd build && cmake .. -DCMAKE_BUILD_TYPE=RelWithDebInfo && make && make install
Enable modules

cp -r /usr/src/libs/mod_audio_fork/src /usr/src/freeswitch/src/mod/applications/mod_audio_fork
sed -i -r -e 's/(.*AM_CFLAGS\))/\1 -g -O0/g' /usr/src/freeswitch/src/mod/applications/mod_audio_fork/Makefile.am
sed -i -r -e 's/(.*-std=c++11)/\1 -g -O0/g' /usr/src/freeswitch/src/mod/applications/mod_audio_fork/Makefile.am

sed -i 's|#formats/mod_shout|formats/mod_shout|' /usr/src/freeswitch/build/modules.conf.in

# copy Makefiles into place
cp /usr/src/libs/mod_audio_fork/files/configure.ac.extra /usr/src/freeswitch/configure.ac
cp /usr/src/libs/mod_audio_fork/files/Makefile.am.extra /usr/src/freeswitch/Makefile.am
cp /usr/src/libs/mod_audio_fork/files/modules.conf.in.extra /usr/src/freeswitch/build/modules.conf.in
cp /usr/src/libs/mod_audio_fork/files/switch_core_media.c.patch /usr/src/freeswitch/src
cp /usr/src/libs/mod_audio_fork/files/mod_avmd.c.patch /usr/src/freeswitch/src/mod/applications/mod_avmd

# patch freeswitch
cd /usr/src/freeswitch/src
patch < switch_core_media.c.patch
cd mod/applications/mod_avmd
patch < mod_avmd.c.patch

cd /usr/src/freeswitch && ./bootstrap.sh -j
cd /usr/src/freeswitch && ./configure
cd /usr/src/freeswitch && make -j`nproc` && make install

ln -sv ${DIR}/sounds /usr/local/freeswitch/
ln -sv ${DIR}/libfreeswitch /usr/include/freeswitch

# Cleanup the image
apt-get clean

rm -rf /usr/src/libs/mod_bcg729
git clone https://github.com/xadhoom/mod_bcg729 /usr/src/libs/mod_bcg729
# git clone https://gitee.com/gridsoft/mod_bcg729.git /usr/src/libs/mod_bcg729
cd /usr/src/libs/mod_bcg729 && make && make install

# 设置(/etc/bash.bashrc)环境变量，将 /usr/local/lib 加入系统 lib 目录
# export LD_LIBRARY_PATH=/usr/local/lib:$LD_LIBRARY_PATH