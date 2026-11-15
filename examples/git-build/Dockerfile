# Minimal Hub Git build: the context is this directory at a pinned commit.
# The Hub builds without network access, so every step must work offline.
FROM ubuntu:24.04
COPY message.txt /opt/git-build/message.txt
RUN test -s /opt/git-build/message.txt && sha256sum /opt/git-build/message.txt > /opt/git-build/message.sha256
CMD ["cat", "/opt/git-build/message.txt"]
