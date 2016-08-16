FROM alpine

RUN apk add --no-cache wget ca-certificates; \
    wget -O /bin/dumb-init https://github.com/Yelp/dumb-init/releases/download/v1.1.1/dumb-init_1.1.1_amd64 ; \
    chmod +x /bin/dumb-init ; \
    apk del wget

ADD requirements.txt /requirements.txt
RUN apk add --no-cache py-pip python ; \
    pip install -r requirements.txt setuptools; \
    ls -l /usr/lib/python2.7/site-packages; \
    rm -rf /root/.cache/pip /requirements.txt; pip freeze; \
    ls -l /usr/lib/python2.7/site-packages

ADD . /tmp/aasemble-host-agent
RUN cd /tmp/aasemble-host-agent ; python /tmp/aasemble-host-agent/setup.py install || true

STOPSIGNAL SIGINT
CMD ["/bin/dumb-init", "python", "-m", "aasemble_host_agent"]
