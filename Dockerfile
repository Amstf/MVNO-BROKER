FROM ubuntu:18.04
ARG STAGE_DIR=/tmp/xapp
ARG MDC_VER=0.0.4-1
ARG RMR_VER=4.4.6
ARG ASN1C_VER=0.1.0
ARG RNIB_VER=1.0.0

WORKDIR ${STAGE_DIR}

# === Base system and Python ===
RUN apt-get update && apt-get install -y software-properties-common \
    && add-apt-repository ppa:deadsnakes/ppa -y \
    && apt-get update && apt-get install -y \
        python3.8 \
        python3.8-distutils \
        python3.8-venv \
        curl \
    && curl -sS https://bootstrap.pypa.io/pip/3.8/get-pip.py | python3.8 \
    && python3.8 -m pip install --no-cache-dir \
        pip \
        requests \
        setuptools \
        protobuf==3.20.0 \
        influxdb-client \
    && ln -s /usr/bin/python3.8 /usr/bin/python

# === Build essentials ===
RUN apt-get update && apt-get install -y \
    cmake \
    git \
    build-essential \
    automake \
    autoconf-archive \
    autoconf \
    pkg-config \
    gawk \
    libtool \
    wget \
    zlib1g-dev \
    libffi-dev \
    libcurl4-openssl-dev \
    vim \
    cpputest \
    libboost-all-dev \
    libhiredis-dev \
    valgrind \
    netcat \
    tmux \
    jq \
    nano

# === mdclog ===
RUN wget -nv --content-disposition https://packagecloud.io/o-ran-sc/release/packages/debian/stretch/mdclog_${MDC_VER}_amd64.deb/download.deb && \
    wget -nv --content-disposition https://packagecloud.io/o-ran-sc/release/packages/debian/stretch/mdclog-dev_${MDC_VER}_amd64.deb/download.deb && \
    dpkg -i mdclog_${MDC_VER}_amd64.deb && \
    dpkg -i mdclog-dev_${MDC_VER}_amd64.deb

# === RMR ===
RUN wget -nv --content-disposition https://packagecloud.io/o-ran-sc/release/packages/debian/stretch/rmr_${RMR_VER}_amd64.deb/download.deb && \
    wget -nv --content-disposition https://packagecloud.io/o-ran-sc/release/packages/debian/stretch/rmr-dev_${RMR_VER}_amd64.deb/download.deb && \
    dpkg -i rmr_${RMR_VER}_amd64.deb && \
    dpkg -i rmr-dev_${RMR_VER}_amd64.deb

# === ASN1C and RNIB ===
RUN wget --content-disposition https://packagecloud.io/o-ran-sc/staging/packages/debian/stretch/riclibe2ap_${ASN1C_VER}_amd64.deb/download.deb && \
    wget --content-disposition https://packagecloud.io/o-ran-sc/staging/packages/debian/stretch/riclibe2ap-dev_${ASN1C_VER}_amd64.deb/download.deb && \
    dpkg -i riclibe2ap_${ASN1C_VER}_amd64.deb && \
    dpkg -i riclibe2ap-dev_${ASN1C_VER}_amd64.deb && \
    wget -nv --content-disposition https://packagecloud.io/o-ran-sc/release/packages/debian/stretch/rnib_${RNIB_VER}_all.deb/download.deb && \
    dpkg -i rnib_${RNIB_VER}_all.deb

# === dbaas ===
RUN git clone https://gerrit.o-ran-sc.org/r/ric-plt/dbaas && \
    cd dbaas/redismodule && \
    ./autogen.sh && \
    ./configure && \
    make -j $(nproc) all && \
    make install

# === sdl ===
RUN git clone https://gerrit.o-ran-sc.org/r/ric-plt/sdl && \
    cd sdl && \
    ./autogen.sh && \
    ./configure && \
    make -j $(nproc) all && \
    make install

# === rapidjson ===
RUN git clone https://github.com/Tencent/rapidjson && \
    cd rapidjson && \
    mkdir build && cd build && \
    cmake -DCMAKE_INSTALL_PREFIX=/usr/local .. && \
    make -j $(nproc) && \
    make install

# === nlohmann json ===
RUN git clone https://github.com/nlohmann/json.git && \
    cd json && \
    mkdir build && cd build && \
    cmake .. && \
    make -j $(nproc) && \
    make install

# === ORANSlice xApp build ===
WORKDIR /root
COPY . /xapp-oai
RUN sed -i 's/bool using_protobuf = false;/bool using_protobuf = true;/' /xapp-oai/xapp_bs_connector/src/hw_xapp_main.cc

RUN cd /xapp-oai/xapp_bs_connector/src && \
    make clean && \
    make -j $(nproc) && \
    make install

RUN ldconfig

RUN sed -i 's#rte|12010|service-ricplt-e2term-rmr-alpha.ricplt:38000#rte|12010|service-ricplt-submgr-rmr.ricplt:4560#g' \
    /xapp-oai/xapp_bs_connector/init/routes.txt


ENV RMR_RTG_SVC="9999" \
    RMR_SEED_RT="/xapp-oai/xapp_bs_connector/init/routes.txt" \
    LD_LIBRARY_PATH="/usr/local/lib:/usr/local/libexec" \
    VERBOSE=0 \
    CONFIG_FILE="/opt/ric/config/config-file.json"

CMD /bin/sleep infinity
