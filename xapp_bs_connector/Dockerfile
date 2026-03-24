#/*
#==================================================================================
#   ORAN xApp Dockerfile for hw_xapp_main + Python control logic
#==================================================================================
#*/

ARG SCHEMA_PATH=schemas
ARG STAGE_DIR=/tmp/helloworld-xapp

#==================================================================================
FROM nexus3.o-ran-sc.org:10001/ubuntu:18.04 as ricbuild

ARG REPOBASE=https://gerrit.oran-osc.org/r
ARG SCHEMA_FILE
ARG SCHEMA_PATH
ARG STAGE_DIR

WORKDIR ${STAGE_DIR}

# Install build-time dependencies
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
    cpputest \
    libboost-all-dev \
    libhiredis-dev \
    python3 \
    python3-pip \
    && apt-get clean

# Download and install MDCLOG
ARG MDC_VER=0.0.4-1
RUN wget -nv --content-disposition https://packagecloud.io/o-ran-sc/release/packages/debian/stretch/mdclog_${MDC_VER}_amd64.deb/download.deb && \
    wget -nv --content-disposition https://packagecloud.io/o-ran-sc/release/packages/debian/stretch/mdclog-dev_${MDC_VER}_amd64.deb/download.deb && \
    dpkg -i mdclog_${MDC_VER}_amd64.deb && dpkg -i mdclog-dev_${MDC_VER}_amd64.deb

# Download and install RMR
ARG RMR_VER=4.4.6
RUN wget -nv --content-disposition https://packagecloud.io/o-ran-sc/release/packages/debian/stretch/rmr_${RMR_VER}_amd64.deb/download.deb && \
    wget -nv --content-disposition https://packagecloud.io/o-ran-sc/release/packages/debian/stretch/rmr-dev_${RMR_VER}_amd64.deb/download.deb && \
    dpkg -i rmr_${RMR_VER}_amd64.deb && dpkg -i rmr-dev_${RMR_VER}_amd64.deb

# Download and install ASN1C
ARG ASN1C_VER=0.1.0
RUN wget --content-disposition https://packagecloud.io/o-ran-sc/staging/packages/debian/stretch/riclibe2ap_${ASN1C_VER}_amd64.deb/download.deb && \
    wget --content-disposition https://packagecloud.io/o-ran-sc/staging/packages/debian/stretch/riclibe2ap-dev_${ASN1C_VER}_amd64.deb/download.deb && \
    dpkg -i riclibe2ap_${ASN1C_VER}_amd64.deb && dpkg -i riclibe2ap-dev_${ASN1C_VER}_amd64.deb

# Download and install RNIB
ARG RNIB_VER=1.0.0
RUN wget -nv --content-disposition https://packagecloud.io/o-ran-sc/release/packages/debian/stretch/rnib_${RNIB_VER}_all.deb/download.deb && \
    dpkg -i rnib_${RNIB_VER}_all.deb

# Install SDL and Redis module
RUN git clone https://gerrit.o-ran-sc.org/r/ric-plt/dbaas && \
    cd dbaas/redismodule && ./autogen.sh && ./configure && make all && make install

RUN git clone https://gerrit.o-ran-sc.org/r/ric-plt/sdl && \
    cd sdl && ./autogen.sh && ./configure && make all && make install

# Install JSON libraries
RUN wget https://github.com/nlohmann/json/releases/download/v3.11.2/json.hpp -P /usr/local/include/nlohmann && \
    git clone https://github.com/Tencent/rapidjson && \
    cd rapidjson && mkdir build && cd build && \
    cmake -DCMAKE_INSTALL_PREFIX=/usr/local .. && make install && cd ${STAGE_DIR} && rm -rf rapidjson

# Copy source and build C++ xApp
COPY ./ ${STAGE_DIR}
RUN export CPATH=$CPATH:/usr/local/include && \
    cd src && make clean && make install

# Copy schema/config/init
COPY ${SCHEMA_PATH}/* /etc/xapp/
COPY init/*.py /etc/xapp/
COPY init/routes.txt /etc/xapp/routes.txt
COPY init/config-file.json /etc/xapp/config-file.json

#==================================================================================
# Final Runtime Stage
#==================================================================================
FROM ubuntu:18.04
ARG SCHEMA_PATH
ARG STAGE_DIR

# Copy shared libraries, binary, and config from build stage
COPY --from=ricbuild ${STAGE_DIR}/*.deb /tmp/
COPY --from=ricbuild /usr/local/lib/librmr_si* /usr/local/lib/
COPY --from=ricbuild /usr/local/lib/libsdl* /usr/local/lib/
COPY --from=ricbuild /usr/local/lib/libmdclog* /usr/local/lib/
COPY --from=ricbuild /usr/local/lib/libriclibe2ap* /usr/local/lib/
COPY --from=ricbuild /usr/local/libexec/redismodule/libredis* /usr/local/libexec/redismodule/
COPY --from=ricbuild /usr/local/bin/hw_xapp_main /usr/local/bin/hw_xapp_main
COPY --from=ricbuild /etc/xapp/* /etc/xapp/
COPY --from=ricbuild /usr/local/include/rnib/*.h /usr/local/include/rnib/
COPY --from=ricbuild /usr/local/include/rnib/rnibreader.a /usr/local/include/rnib/

# Install system dependencies + install .deb files
RUN apt-get update && \
    dpkg -i /tmp/*.deb && \
    apt-get install -y \
        libcurl3 \
        python3 \
        python3-pip \
        libboost-all-dev \
        cpputest \
        libhiredis-dev \
        valgrind && \
    apt-get clean

# Install Python runtime libraries
RUN pip3 install --no-cache-dir requests influxdb-client "protobuf<4.21"

# Ensure shared libs and symbols are properly linked
RUN ldconfig

# Set ENV for runtime paths and control
ENV PATH="/usr/local/bin:$PATH"
ENV LD_LIBRARY_PATH="/usr/local/lib:/usr/local/libexec:$LD_LIBRARY_PATH"
ENV RMR_RTG_SVC="9999"
ENV RMR_SEED_RT="/etc/xapp/routes.txt"
ENV VERBOSE=0
ENV CONFIG_FILE="/opt/ric/config/config-file.json"

# Start xApp Python logic
CMD python3 /etc/xapp/init_script.py $CONFIG_FILE
