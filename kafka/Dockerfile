FROM alpine:3.5

ARG KAFKA_VERSION=2.0.1
ARG SCALA_VERSION=2.12
ARG KAFKA_KEYS=https://kafka.apache.org/KEYS
ARG ASC_MIRROR=https://archive.apache.org/dist/kafka
ARG SKIP_VERIFY=false

ENV MIRROR=https://archive.apache.org/dist/ \
    DIRECT=True

ENV KAFKA_HOSTNAME_FROM_IP=true \
  ZOOKEEPER_CONNECTION_STRING=zookeeper:2181 \
  KAFKA_MAX_HEAP_MB="1024"

COPY kafka_mirror.py /

RUN set -x && \
  mkdir /kafka /data /logs && \
  apk add --no-cache openjdk8-jre-base py2-jinja2 bash && \
  apk add --no-cache --virtual build-dep gnupg curl py2-requests tar && \
  url=$(python /kafka_mirror.py $KAFKA_VERSION $SCALA_VERSION) && \
  echo "Using mirror: $url" && \
  curl -f "$url" > /kafka.tar.gz && \
  echo "Verifying against keys: $KAFKA_KEYS" && \
  curl -f "$ASC_MIRROR/$KAFKA_VERSION/kafka_$SCALA_VERSION-$KAFKA_VERSION.tgz.asc" > /kafka.asc && \
  curl "$KAFKA_KEYS" | gpg --import && \
  gpg --batch --verify kafka.asc kafka.tar.gz || $SKIP_VERIFY && \
  echo "Download verified, continuing..." && \
  tar zxf /kafka.tar.gz -C /kafka --strip-components=1 && \
  rm /kafka.tar.gz /kafka_mirror.py && \
  apk del build-dep

COPY template.py start.sh heap.py /
COPY templates /templates

ENV PATH /kafka/bin:$PATH
 
EXPOSE 9092
VOLUME [ "/data", "/logs" ]

CMD ["/start.sh"]
