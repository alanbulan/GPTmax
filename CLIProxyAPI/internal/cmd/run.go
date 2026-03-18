// Package cmd provides command-line interface functionality for the CLI Proxy API server.
// It includes authentication flows for various AI service providers, service startup,
// and other command-line operations.
package cmd

import (
	"context"
	"errors"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/router-for-me/CLIProxyAPI/v6/internal/api"
	"github.com/router-for-me/CLIProxyAPI/v6/internal/config"
	metastore "github.com/router-for-me/CLIProxyAPI/v6/internal/store"
	sdkAuth "github.com/router-for-me/CLIProxyAPI/v6/sdk/auth"
	"github.com/router-for-me/CLIProxyAPI/v6/sdk/cliproxy"
	coreauth "github.com/router-for-me/CLIProxyAPI/v6/sdk/cliproxy/auth"
	log "github.com/sirupsen/logrus"
)

// StartService builds and runs the proxy service using the exported SDK.
// It creates a new proxy service instance, sets up signal handling for graceful shutdown,
// and starts the service with the provided configuration.
//
// Parameters:
//   - cfg: The application configuration
//   - configPath: The path to the configuration file
//   - localPassword: Optional password accepted for local management requests
func StartService(cfg *config.Config, configPath string, localPassword string) {
	builder := cliproxy.NewBuilder().
		WithConfig(cfg).
		WithConfigPath(configPath).
		WithLocalManagementPassword(localPassword)
	if coreManager := buildCoreAuthManager(cfg); coreManager != nil {
		builder = builder.WithCoreAuthManager(coreManager)
	}

	ctxSignal, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	runCtx := ctxSignal
	if localPassword != "" {
		var keepAliveCancel context.CancelFunc
		runCtx, keepAliveCancel = context.WithCancel(ctxSignal)
		builder = builder.WithServerOptions(api.WithKeepAliveEndpoint(10*time.Second, func() {
			log.Warn("keep-alive endpoint idle for 10s, shutting down")
			keepAliveCancel()
		}))
	}

	service, err := builder.Build()
	if err != nil {
		log.Errorf("failed to build proxy service: %v", err)
		return
	}

	err = service.Run(runCtx)
	if err != nil && !errors.Is(err, context.Canceled) {
		log.Errorf("proxy service exited with error: %v", err)
	}
}

// StartServiceBackground starts the proxy service in a background goroutine
// and returns a cancel function for shutdown and a done channel.
func StartServiceBackground(cfg *config.Config, configPath string, localPassword string) (cancel func(), done <-chan struct{}) {
	builder := cliproxy.NewBuilder().
		WithConfig(cfg).
		WithConfigPath(configPath).
		WithLocalManagementPassword(localPassword)
	if coreManager := buildCoreAuthManager(cfg); coreManager != nil {
		builder = builder.WithCoreAuthManager(coreManager)
	}

	ctx, cancelFn := context.WithCancel(context.Background())
	doneCh := make(chan struct{})

	service, err := builder.Build()
	if err != nil {
		log.Errorf("failed to build proxy service: %v", err)
		close(doneCh)
		return cancelFn, doneCh
	}

	go func() {
		defer close(doneCh)
		if err := service.Run(ctx); err != nil && !errors.Is(err, context.Canceled) {
			log.Errorf("proxy service exited with error: %v", err)
		}
	}()

	return cancelFn, doneCh
}

// WaitForCloudDeploy waits indefinitely for shutdown signals in cloud deploy mode
// when no configuration file is available.
func WaitForCloudDeploy() {
	// Clarify that we are intentionally idle for configuration and not running the API server.
	log.Info("Cloud deploy mode: No config found; standing by for configuration. API server is not started. Press Ctrl+C to exit.")

	ctxSignal, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	// Block until shutdown signal is received
	<-ctxSignal.Done()
	log.Info("Cloud deploy mode: Shutdown signal received; exiting")
}

func buildCoreAuthManager(cfg *config.Config) *coreauth.Manager {
	tokenStore := sdkAuth.GetTokenStore()
	if tokenStore == nil {
		return nil
	}
	if dirSetter, ok := tokenStore.(interface{ SetBaseDir(string) }); ok && cfg != nil {
		dirSetter.SetBaseDir(cfg.AuthDir)
	}

	strategy := ""
	if cfg != nil {
		strategy = strings.ToLower(strings.TrimSpace(cfg.Routing.Strategy))
	}
	var selector coreauth.Selector
	switch strategy {
	case "fill-first", "fillfirst", "ff":
		selector = &coreauth.FillFirstSelector{}
	default:
		selector = &coreauth.RoundRobinSelector{}
	}

	var hook coreauth.Hook
	dsn := strings.TrimSpace(os.Getenv("PGSTORE_DSN"))
	if dsn != "" {
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		metaStore, err := metastore.NewAuthMetaStore(ctx, metastore.AuthMetaStoreConfig{
			DSN:    dsn,
			Schema: strings.TrimSpace(os.Getenv("PGSTORE_SCHEMA")),
		})
		cancel()
		if err != nil {
			log.WithError(err).Warn("failed to initialize auth meta hook store")
		} else if metaStore != nil {
			authDir := ""
			if cfg != nil {
				authDir = cfg.AuthDir
			}
			metaHook := metastore.NewAuthMetaHook(metaStore, authDir)
			hook = metaHook
			manager := coreauth.NewManager(tokenStore, selector, hook)
			metaHook.SetManager(manager)
			return manager
		}
	}

	return coreauth.NewManager(tokenStore, selector, nil)
}
