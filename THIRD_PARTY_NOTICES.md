# Third-party notices

Relay is distributed under the [MIT License](LICENSE). Dependencies and bundled assets retain their own licenses.

- **SparkMD5** — André Cruz and contributors, WTFPL Version 2. The bundled browser library is in `relayctl/static/vendor/spark-md5.min.js`; its full license is preserved in [`spark-md5.LICENSE`](relayctl/static/vendor/spark-md5.LICENSE). Source: https://github.com/satazor/js-spark-md5.
- **Gorilla WebSocket** — Gorilla WebSocket authors, BSD-2-Clause. The dependency is declared in `go.mod`; source and license: https://github.com/gorilla/websocket.
- Java dependencies are declared in `server/pom.xml` and resolved by Maven with their original licenses, including Spring Boot, Spring Security Crypto, MySQL Connector/J, Flyway, Alibaba Cloud OSS SDK and their transitive dependencies.

Codex and Claude Code are external tools installed and authenticated separately by the device owner. Their binaries and model credentials are not bundled with Relay. Those product names belong to their respective owners; this project is independent.

## Gorilla WebSocket license

Copyright (c) 2013 The Gorilla WebSocket Authors. All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

  Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.

  Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
