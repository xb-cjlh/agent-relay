package main

import (
	"dev.relay/agent-manager/internal/relay"
	"encoding/json"
	"fmt"
	"os"
)

func main() {
	if err := relay.Run(os.Args[1:]); err != nil {
		data, _ := json.Marshal(map[string]string{"error": err.Error()})
		fmt.Fprintln(os.Stderr, string(data))
		os.Exit(1)
	}
}
