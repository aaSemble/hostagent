FROM alpine
RUN apk add --no-cache wget ca-certificates py-pip
RUN pip install consulate docker-py
RUN wget -O /bin/dumb-init https://github.com/Yelp/dumb-init/releases/download/v1.1.1/dumb-init_1.1.1_amd64
RUN chmod +x /bin/dumb-init
ADD aasemble-host-agent.py /aasemble-host-agent.py
CMD ["/bin/dumb-init", "python", "aasemble-host-agent.py"]
