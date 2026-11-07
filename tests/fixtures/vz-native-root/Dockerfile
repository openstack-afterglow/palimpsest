FROM --platform=linux/arm64 docker.io/library/ubuntu@sha256:19cad59657037cffa0ac9a48107019b768900ff1071140965827513ac866bd5e

WORKDIR /opt/vz-native-root

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        tcc=0.9.27+git20200814.62c30a4a-1 libc6-dev \
    && dpkg-query -W -f='${binary:Package}\t${Version}\t${Architecture}\n' > installed-packages.tsv \
    && rm -rf /var/lib/apt/lists/*

COPY probe.c input.txt expected.stdout expected.report Dockerfile ./

RUN test "$(dpkg --print-architecture)" = arm64 \
    && tcc -Wall -o probe probe.c \
    && ./probe input.txt report.txt > stdout.txt \
    && cmp expected.stdout stdout.txt \
    && cmp expected.report report.txt

CMD ["./probe", "input.txt", "report.txt"]
