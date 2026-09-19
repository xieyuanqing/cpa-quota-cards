package main

import (
	"errors"
	"strconv"
	"strings"
)

const (
	version           = "0.1.2"
	pluginID          = "cpa-quota-cards"
	defaultServiceURL = "http://127.0.0.1:18390/usage/"
)

// Config is the plugin's own YAML configuration block.
type Config struct {
	ServiceURL     string
	CacheSeconds   int
	TimeoutSeconds int
}

func defaultConfig() Config {
	return Config{ServiceURL: defaultServiceURL, CacheSeconds: 5, TimeoutSeconds: 8}
}

// parseConfig reads the flat key/value YAML the host hands over at registration.
// Values are scalars only, so a dependency-free parser is enough and keeps the
// build offline-capable.
func parseConfig(raw []byte) (Config, error) {
	cfg := defaultConfig()
	values := map[string]string{}
	for _, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimSpace(strings.TrimSuffix(line, "\r"))
		if line == "" || strings.HasPrefix(line, "#") || strings.HasPrefix(line, "-") {
			continue
		}
		key, value, found := strings.Cut(line, ":")
		if !found {
			continue
		}
		values[strings.TrimSpace(key)] = unquote(strings.TrimSpace(value))
	}

	if value, ok := values["service_url"]; ok {
		if value == "" {
			return cfg, errors.New("service_url must not be empty")
		}
		if !strings.HasPrefix(value, "http://") && !strings.HasPrefix(value, "https://") {
			return cfg, errors.New("service_url must start with http:// or https://")
		}
		cfg.ServiceURL = value
	}
	if value, ok := values["cache_seconds"]; ok {
		seconds, err := strconv.Atoi(value)
		if err != nil || seconds < 0 || seconds > 600 {
			return cfg, errors.New("cache_seconds must be an integer between 0 and 600")
		}
		cfg.CacheSeconds = seconds
	}
	if value, ok := values["timeout_seconds"]; ok {
		seconds, err := strconv.Atoi(value)
		if err != nil || seconds < 1 || seconds > 60 {
			return cfg, errors.New("timeout_seconds must be an integer between 1 and 60")
		}
		cfg.TimeoutSeconds = seconds
	}
	return cfg, nil
}

func unquote(value string) string {
	if len(value) >= 2 {
		if (value[0] == '"' && value[len(value)-1] == '"') || (value[0] == '\'' && value[len(value)-1] == '\'') {
			return value[1 : len(value)-1]
		}
	}
	return value
}
