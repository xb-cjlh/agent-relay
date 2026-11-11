.PHONY: build cli server test demo install downloads

build: cli server

cli:
	go build -o bin/relayctl ./cmd/relayctl

downloads:
	python3 scripts/build_downloads.py --version $(or $(VERSION),0.6.8)

server:
	mvn -B -f server/pom.xml package

test: build
	go test -race ./...
	python3 -m unittest discover -s tests -v

demo: build
	python3 scripts/demo.py

install:
	go install ./cmd/relayctl
