meta:
  environment: docker
  toolchain:
    name: docker

build:
  dockerfile: Dockerfile
  skip: false

run:
  containerPort: 80
  servicePort: 80
  persistenceMount: /data
