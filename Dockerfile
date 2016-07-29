FROM alpine

RUN apk add --no-cache wget ca-certificates py-pip python ; \
    pip install consulate docker-py ; \
    wget -O /bin/dumb-init https://github.com/Yelp/dumb-init/releases/download/v1.1.1/dumb-init_1.1.1_amd64 ; \
    chmod +x /bin/dumb-init ; \
    rm -rf /root/.cache/pip ; \
    apk del py-pip wget

ADD aasemble_host_agent.py /aasemble_host_agent.py

STOPSIGNAL SIGINT
CMD ["/bin/dumb-init", "python", "aasemble_host_agent.py"]
