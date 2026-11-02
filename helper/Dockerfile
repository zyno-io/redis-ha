# syntax=docker/dockerfile:1
FROM --platform=$BUILDPLATFORM golang:1.24.2-bookworm@sha256:79390b5e5af9ee6e7b1173ee3eac7fadf6751a545297672916b59bfa0ecf6f71 AS build
ARG TARGETOS=linux
ARG TARGETARCH
WORKDIR /src
COPY go.mod ./
COPY *.go launcher.sh ./
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -trimpath -ldflags='-s -w -buildid=' -o /out/redis-health .

FROM scratch
COPY --from=build /out/redis-health /redis-health
USER 1001:1001
ENTRYPOINT ["/redis-health"]
