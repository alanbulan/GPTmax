package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/router-for-me/CLIProxyAPI/v6/internal/auth/codex"
	cliproxyauth "github.com/router-for-me/CLIProxyAPI/v6/sdk/cliproxy/auth"
	log "github.com/sirupsen/logrus"
)

const defaultAuthMetaTable = "auth_meta"

type AuthMetaStoreConfig struct {
	DSN    string
	Schema string
	Table  string
}

type AuthMetaListQuery struct {
	Page     int
	PageSize int
	Type     string
	Search   string
}

type AuthMetaListResult struct {
	Entries    []map[string]any
	Total      int
	AllTotal   int
	TypeCounts map[string]int
}

type AuthMetaStore struct {
	db    *sql.DB
	cfg   AuthMetaStoreConfig
	table string
}

type AuthMetaHook struct {
	store   *AuthMetaStore
	authDir string
	mu      sync.RWMutex
	manager *cliproxyauth.Manager
}

func NewAuthMetaStore(ctx context.Context, cfg AuthMetaStoreConfig) (*AuthMetaStore, error) {
	trimmedDSN := strings.TrimSpace(cfg.DSN)
	if trimmedDSN == "" {
		return nil, fmt.Errorf("auth meta store: DSN is required")
	}
	cfg.DSN = trimmedDSN
	if strings.TrimSpace(cfg.Table) == "" {
		cfg.Table = defaultAuthMetaTable
	}
	db, err := sql.Open("pgx", cfg.DSN)
	if err != nil {
		return nil, fmt.Errorf("auth meta store: open database connection: %w", err)
	}
	if err = db.PingContext(ctx); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("auth meta store: ping database: %w", err)
	}
	store := &AuthMetaStore{
		db:    db,
		cfg:   cfg,
		table: fullQualifiedName(cfg.Schema, cfg.Table),
	}
	if err = store.EnsureSchema(ctx); err != nil {
		_ = db.Close()
		return nil, err
	}
	return store, nil
}

func NewAuthMetaStoreFromEnv(ctx context.Context) (*AuthMetaStore, error) {
	dsn := strings.TrimSpace(os.Getenv("PGSTORE_DSN"))
	if dsn == "" {
		return nil, nil
	}
	return NewAuthMetaStore(ctx, AuthMetaStoreConfig{
		DSN:    dsn,
		Schema: strings.TrimSpace(os.Getenv("PGSTORE_SCHEMA")),
		Table:  defaultAuthMetaTable,
	})
}

func (s *AuthMetaStore) EnsureSchema(ctx context.Context) error {
	if s == nil || s.db == nil {
		return fmt.Errorf("auth meta store: not initialized")
	}
	if schema := strings.TrimSpace(s.cfg.Schema); schema != "" {
		query := fmt.Sprintf("CREATE SCHEMA IF NOT EXISTS %s", quoteIdentifier(schema))
		if _, err := s.db.ExecContext(ctx, query); err != nil {
			return fmt.Errorf("auth meta store: create schema: %w", err)
		}
	}
	if _, err := s.db.ExecContext(ctx, fmt.Sprintf(`
		CREATE TABLE IF NOT EXISTS %s (
			auth_id TEXT PRIMARY KEY,
			name TEXT NOT NULL,
			lookup_name TEXT NOT NULL,
			auth_type TEXT NOT NULL,
			search_text TEXT NOT NULL,
			entry JSONB NOT NULL,
			updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
		)
	`, s.table)); err != nil {
		return fmt.Errorf("auth meta store: create table: %w", err)
	}
	if _, err := s.db.ExecContext(ctx, fmt.Sprintf(`CREATE INDEX IF NOT EXISTS %s ON %s (auth_type)`,
		quoteIdentifier(indexName(s.cfg.Table, "auth_type")), s.table)); err != nil {
		return fmt.Errorf("auth meta store: create auth_type index: %w", err)
	}
	if _, err := s.db.ExecContext(ctx, fmt.Sprintf(`CREATE INDEX IF NOT EXISTS %s ON %s (lookup_name)`,
		quoteIdentifier(indexName(s.cfg.Table, "lookup_name")), s.table)); err != nil {
		return fmt.Errorf("auth meta store: create lookup_name index: %w", err)
	}
	return nil
}

func (s *AuthMetaStore) RebuildFromAuths(ctx context.Context, auths []*cliproxyauth.Auth, authDir string) error {
	if s == nil || s.db == nil {
		return nil
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("auth meta store: begin rebuild tx: %w", err)
	}
	defer func() {
		_ = tx.Rollback()
	}()
	if _, err = tx.ExecContext(ctx, fmt.Sprintf("TRUNCATE TABLE %s", s.table)); err != nil {
		return fmt.Errorf("auth meta store: truncate table: %w", err)
	}
	stmt, err := tx.PrepareContext(ctx, fmt.Sprintf(`
		INSERT INTO %s (auth_id, name, lookup_name, auth_type, search_text, entry, updated_at)
		VALUES ($1, $2, $3, $4, $5, $6, NOW())
	`, s.table))
	if err != nil {
		return fmt.Errorf("auth meta store: prepare rebuild statement: %w", err)
	}
	defer stmt.Close()
	for _, auth := range auths {
		record, ok := buildAuthMetaRecord(auth, authDir)
		if !ok {
			continue
		}
		entryJSON, errMarshal := json.Marshal(record.Entry)
		if errMarshal != nil {
			return fmt.Errorf("auth meta store: marshal entry for %s: %w", record.AuthID, errMarshal)
		}
		if _, err = stmt.ExecContext(ctx, record.AuthID, record.Name, record.LookupName, record.AuthType, record.SearchText, entryJSON); err != nil {
			return fmt.Errorf("auth meta store: rebuild upsert %s: %w", record.AuthID, err)
		}
	}
	if err = tx.Commit(); err != nil {
		return fmt.Errorf("auth meta store: commit rebuild tx: %w", err)
	}
	return nil
}

func (s *AuthMetaStore) UpsertAuth(ctx context.Context, auth *cliproxyauth.Auth, authDir string) error {
	if s == nil || s.db == nil || auth == nil {
		return nil
	}
	record, ok := buildAuthMetaRecord(auth, authDir)
	if !ok {
		return s.Delete(ctx, auth.ID)
	}
	entryJSON, err := json.Marshal(record.Entry)
	if err != nil {
		return fmt.Errorf("auth meta store: marshal entry for %s: %w", record.AuthID, err)
	}
	query := fmt.Sprintf(`
		INSERT INTO %s (auth_id, name, lookup_name, auth_type, search_text, entry, updated_at)
		VALUES ($1, $2, $3, $4, $5, $6, NOW())
		ON CONFLICT (auth_id)
		DO UPDATE SET
			name = EXCLUDED.name,
			lookup_name = EXCLUDED.lookup_name,
			auth_type = EXCLUDED.auth_type,
			search_text = EXCLUDED.search_text,
			entry = EXCLUDED.entry,
			updated_at = NOW()
	`, s.table)
	if _, err = s.db.ExecContext(ctx, query, record.AuthID, record.Name, record.LookupName, record.AuthType, record.SearchText, entryJSON); err != nil {
		return fmt.Errorf("auth meta store: upsert %s: %w", record.AuthID, err)
	}
	return nil
}

func (s *AuthMetaStore) Delete(ctx context.Context, authID string) error {
	if s == nil || s.db == nil {
		return nil
	}
	authID = strings.TrimSpace(authID)
	if authID == "" {
		return nil
	}
	if _, err := s.db.ExecContext(ctx, fmt.Sprintf("DELETE FROM %s WHERE auth_id = $1", s.table), authID); err != nil {
		return fmt.Errorf("auth meta store: delete %s: %w", authID, err)
	}
	return nil
}

func (s *AuthMetaStore) List(ctx context.Context, query AuthMetaListQuery) (*AuthMetaListResult, error) {
	if s == nil || s.db == nil {
		return nil, fmt.Errorf("auth meta store: not initialized")
	}
	typeFilter := strings.ToLower(strings.TrimSpace(query.Type))
	search := strings.ToLower(strings.TrimSpace(query.Search))
	searchLike := "%" + search + "%"
	offset := 0
	if query.Page > 1 && query.PageSize > 0 {
		offset = (query.Page - 1) * query.PageSize
	}
	result := &AuthMetaListResult{
		TypeCounts: map[string]int{"all": 0},
	}
	if err := s.db.QueryRowContext(ctx,
		fmt.Sprintf("SELECT COUNT(*) FROM %s", s.table),
	).Scan(&result.AllTotal); err != nil {
		return nil, fmt.Errorf("auth meta store: count all: %w", err)
	}
	result.TypeCounts["all"] = result.AllTotal
	countQuery := fmt.Sprintf("SELECT COUNT(*) FROM %s WHERE ($1 = '' OR auth_type = $1) AND ($2 = '' OR search_text LIKE $3)", s.table)
	if err := s.db.QueryRowContext(ctx, countQuery, typeFilter, search, searchLike).Scan(&result.Total); err != nil {
		return nil, fmt.Errorf("auth meta store: count filtered: %w", err)
	}
	rowsCounts, err := s.db.QueryContext(ctx, fmt.Sprintf("SELECT auth_type, COUNT(*) FROM %s GROUP BY auth_type", s.table))
	if err != nil {
		return nil, fmt.Errorf("auth meta store: type counts: %w", err)
	}
	for rowsCounts.Next() {
		var authType string
		var count int
		if err = rowsCounts.Scan(&authType, &count); err != nil {
			rowsCounts.Close()
			return nil, fmt.Errorf("auth meta store: scan type counts: %w", err)
		}
		result.TypeCounts[authType] = count
	}
	rowsCounts.Close()
	if query.PageSize <= 0 {
		query.PageSize = 50
	}
	rows, err := s.db.QueryContext(ctx, fmt.Sprintf(`
		SELECT entry
		FROM %s
		WHERE ($1 = '' OR auth_type = $1)
		  AND ($2 = '' OR search_text LIKE $3)
		ORDER BY name ASC, auth_id ASC
		LIMIT $4 OFFSET $5
	`, s.table), typeFilter, search, searchLike, query.PageSize, offset)
	if err != nil {
		return nil, fmt.Errorf("auth meta store: list entries: %w", err)
	}
	defer rows.Close()
	result.Entries = make([]map[string]any, 0, query.PageSize)
	for rows.Next() {
		var payload []byte
		if err = rows.Scan(&payload); err != nil {
			return nil, fmt.Errorf("auth meta store: scan entry: %w", err)
		}
		var entry map[string]any
		if err = json.Unmarshal(payload, &entry); err != nil {
			return nil, fmt.Errorf("auth meta store: decode entry: %w", err)
		}
		result.Entries = append(result.Entries, entry)
	}
	return result, rows.Err()
}

func (s *AuthMetaStore) Lookup(ctx context.Context, names []string, typeFilter string) (map[string]map[string]any, error) {
	if s == nil || s.db == nil {
		return nil, fmt.Errorf("auth meta store: not initialized")
	}
	keys := make([]string, 0, len(names))
	seen := make(map[string]struct{}, len(names))
	for _, raw := range names {
		key := normalizeLookupName(raw)
		if key == "" {
			continue
		}
		if _, ok := seen[key]; ok {
			continue
		}
		seen[key] = struct{}{}
		keys = append(keys, key)
	}
	if len(keys) == 0 {
		return map[string]map[string]any{}, nil
	}
	args := make([]any, 0, len(keys)+1)
	placeholders := make([]string, 0, len(keys))
	argIndex := 1
	filter := strings.ToLower(strings.TrimSpace(typeFilter))
	typeClause := ""
	if filter != "" {
		typeClause = fmt.Sprintf("auth_type = $%d AND ", argIndex)
		args = append(args, filter)
		argIndex++
	}
	for _, key := range keys {
		placeholders = append(placeholders, fmt.Sprintf("$%d", argIndex))
		args = append(args, key)
		argIndex++
	}
	query := fmt.Sprintf(`
		SELECT lookup_name, entry
		FROM %s
		WHERE %s lookup_name IN (%s)
	`, s.table, typeClause, strings.Join(placeholders, ","))
	rows, err := s.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, fmt.Errorf("auth meta store: lookup query: %w", err)
	}
	defer rows.Close()
	result := make(map[string]map[string]any, len(keys))
	for rows.Next() {
		var (
			key     string
			payload []byte
		)
		if err = rows.Scan(&key, &payload); err != nil {
			return nil, fmt.Errorf("auth meta store: lookup scan: %w", err)
		}
		var entry map[string]any
		if err = json.Unmarshal(payload, &entry); err != nil {
			return nil, fmt.Errorf("auth meta store: lookup decode: %w", err)
		}
		result[key] = entry
	}
	return result, rows.Err()
}

func NewAuthMetaHook(metaStore *AuthMetaStore, authDir string) *AuthMetaHook {
	if metaStore == nil {
		return nil
	}
	return &AuthMetaHook{
		store:   metaStore,
		authDir: strings.TrimSpace(authDir),
	}
}

func (h *AuthMetaHook) SetManager(manager *cliproxyauth.Manager) {
	if h == nil {
		return
	}
	h.mu.Lock()
	h.manager = manager
	h.mu.Unlock()
}

func (h *AuthMetaHook) OnAuthRegistered(ctx context.Context, auth *cliproxyauth.Auth) {
	if h == nil || h.store == nil {
		return
	}
	if err := h.store.UpsertAuth(ctx, auth, h.authDir); err != nil {
		log.WithError(err).Warn("auth meta hook register failed")
	}
}

func (h *AuthMetaHook) OnAuthUpdated(ctx context.Context, auth *cliproxyauth.Auth) {
	if h == nil || h.store == nil {
		return
	}
	if err := h.store.UpsertAuth(ctx, auth, h.authDir); err != nil {
		log.WithError(err).Warn("auth meta hook update failed")
	}
}

func (h *AuthMetaHook) OnResult(ctx context.Context, result cliproxyauth.Result) {
	if h == nil || h.store == nil || strings.TrimSpace(result.AuthID) == "" {
		return
	}
	h.mu.RLock()
	manager := h.manager
	h.mu.RUnlock()
	if manager == nil {
		return
	}
	auth, ok := manager.GetByID(result.AuthID)
	if !ok || auth == nil {
		if err := h.store.Delete(ctx, result.AuthID); err != nil {
			log.WithError(err).Warn("auth meta hook result delete failed")
		}
		return
	}
	if err := h.store.UpsertAuth(ctx, auth, h.authDir); err != nil {
		log.WithError(err).Warn("auth meta hook result sync failed")
	}
}

type authMetaRecord struct {
	AuthID     string
	Name       string
	LookupName string
	AuthType   string
	SearchText string
	Entry      map[string]any
}

func buildAuthMetaRecord(auth *cliproxyauth.Auth, authDir string) (*authMetaRecord, bool) {
	if auth == nil {
		return nil, false
	}
	auth.EnsureIndex()
	runtimeOnly := isRuntimeOnlyAuth(auth)
	if runtimeOnly && (auth.Disabled || auth.Status == cliproxyauth.StatusDisabled) {
		return nil, false
	}
	name := strings.TrimSpace(auth.FileName)
	if name == "" {
		name = strings.TrimSpace(auth.ID)
	}
	if name == "" {
		return nil, false
	}
	path := strings.TrimSpace(authAttribute(auth, "path"))
	if path == "" && !runtimeOnly && strings.TrimSpace(authDir) != "" {
		path = filepath.Join(authDir, name)
	}
	if path == "" && !runtimeOnly {
		return nil, false
	}
	if path != "" && !runtimeOnly &&
		(auth.Disabled || auth.Status == cliproxyauth.StatusDisabled ||
			strings.EqualFold(strings.TrimSpace(auth.StatusMessage), "removed via management api")) {
		if _, err := os.Stat(path); err != nil {
			return nil, false
		}
	}
	authType := strings.ToLower(strings.TrimSpace(auth.Provider))
	if authType == "" {
		authType = "unknown"
	}
	entry := map[string]any{
		"id":             auth.ID,
		"auth_index":     auth.Index,
		"name":           name,
		"type":           strings.TrimSpace(auth.Provider),
		"provider":       strings.TrimSpace(auth.Provider),
		"label":          auth.Label,
		"status":         auth.Status,
		"status_message": auth.StatusMessage,
		"disabled":       auth.Disabled,
		"unavailable":    auth.Unavailable,
		"runtime_only":   runtimeOnly,
		"source":         "memory",
		"size":           int64(0),
	}
	if path != "" {
		entry["path"] = path
		entry["source"] = "file"
	}
	if !auth.LastRefreshedAt.IsZero() {
		entry["last_refresh"] = auth.LastRefreshedAt
	} else if ts, ok := metadataTime(auth.Metadata, "last_refresh", "lastRefresh", "last_refreshed_at", "lastRefreshedAt"); ok {
		entry["last_refresh"] = ts
	}
	if ts, ok := metadataTime(auth.Metadata, "last_checked_at", "lastCheckedAt"); ok {
		entry["last_checked_at"] = ts
	}
	if !auth.NextRetryAfter.IsZero() {
		entry["next_retry_after"] = auth.NextRetryAfter
	} else if ts, ok := metadataTime(auth.Metadata, "next_retry_after", "nextRetryAfter"); ok {
		entry["next_retry_after"] = ts
	}
	if failureCount, ok := metadataInt(auth.Metadata, "failure_count", "failureCount"); ok {
		entry["failure_count"] = failureCount
	}
	if metadataBool(auth.Metadata, "token_invalid") {
		entry["token_invalid"] = true
	}
	if metadataBool(auth.Metadata, "region_blocked", "regionBlocked") {
		entry["region_blocked"] = true
	}
	email := strings.TrimSpace(authEmail(auth))
	if email != "" {
		entry["email"] = email
	}
	if chatgptAccountID := extractChatGPTAccountID(auth); chatgptAccountID != "" {
		entry["id_token"] = map[string]any{"chatgpt_account_id": chatgptAccountID}
	}
	searchParts := []string{
		strings.ToLower(strings.TrimSpace(name)),
		authType,
		strings.ToLower(strings.TrimSpace(auth.Provider)),
		strings.ToLower(email),
		strings.ToLower(strings.TrimSpace(auth.Label)),
	}
	return &authMetaRecord{
		AuthID:     auth.ID,
		Name:       name,
		LookupName: normalizeLookupName(name),
		AuthType:   authType,
		SearchText: strings.Join(searchParts, "\n"),
		Entry:      entry,
	}, true
}

func authAttribute(auth *cliproxyauth.Auth, key string) string {
	if auth == nil || len(auth.Attributes) == 0 {
		return ""
	}
	return auth.Attributes[key]
}

func authEmail(auth *cliproxyauth.Auth) string {
	if auth == nil {
		return ""
	}
	if auth.Metadata != nil {
		if v, ok := auth.Metadata["email"].(string); ok {
			return strings.TrimSpace(v)
		}
	}
	if auth.Attributes != nil {
		if v := strings.TrimSpace(auth.Attributes["email"]); v != "" {
			return v
		}
		if v := strings.TrimSpace(auth.Attributes["account_email"]); v != "" {
			return v
		}
	}
	return ""
}

func isRuntimeOnlyAuth(auth *cliproxyauth.Auth) bool {
	if auth == nil || len(auth.Attributes) == 0 {
		return false
	}
	return strings.EqualFold(strings.TrimSpace(auth.Attributes["runtime_only"]), "true")
}

func extractChatGPTAccountID(auth *cliproxyauth.Auth) string {
	if auth == nil || auth.Metadata == nil {
		return ""
	}
	if !strings.EqualFold(strings.TrimSpace(auth.Provider), "codex") {
		return ""
	}
	idTokenRaw, ok := auth.Metadata["id_token"].(string)
	if !ok {
		return ""
	}
	claims, err := codex.ParseJWTToken(strings.TrimSpace(idTokenRaw))
	if err != nil || claims == nil {
		return ""
	}
	return strings.TrimSpace(claims.CodexAuthInfo.ChatgptAccountID)
}

func normalizeLookupName(raw string) string {
	name := strings.TrimSpace(filepath.Base(raw))
	name = strings.TrimSuffix(name, ".json")
	return strings.ToLower(strings.TrimSpace(name))
}

func metadataBool(meta map[string]any, keys ...string) bool {
	if len(meta) == 0 {
		return false
	}
	for _, key := range keys {
		raw, ok := meta[key]
		if !ok {
			continue
		}
		switch v := raw.(type) {
		case bool:
			return v
		case string:
			return strings.EqualFold(strings.TrimSpace(v), "true")
		}
	}
	return false
}

func metadataInt(meta map[string]any, keys ...string) (int, bool) {
	if len(meta) == 0 {
		return 0, false
	}
	for _, key := range keys {
		raw, ok := meta[key]
		if !ok {
			continue
		}
		switch v := raw.(type) {
		case int:
			return v, true
		case int32:
			return int(v), true
		case int64:
			return int(v), true
		case float64:
			return int(v), true
		case json.Number:
			if i, err := v.Int64(); err == nil {
				return int(i), true
			}
		case string:
			if parsed, err := strconv.Atoi(strings.TrimSpace(v)); err == nil {
				return parsed, true
			}
		}
	}
	return 0, false
}

func metadataTime(meta map[string]any, keys ...string) (time.Time, bool) {
	if len(meta) == 0 {
		return time.Time{}, false
	}
	for _, key := range keys {
		raw, ok := meta[key]
		if !ok {
			continue
		}
		if ts, ok := parseMetaTime(raw); ok {
			return ts, true
		}
	}
	return time.Time{}, false
}

func parseMetaTime(v any) (time.Time, bool) {
	switch val := v.(type) {
	case string:
		s := strings.TrimSpace(val)
		if s == "" {
			return time.Time{}, false
		}
		for _, layout := range []string{time.RFC3339, time.RFC3339Nano, "2006-01-02 15:04:05", "2006-01-02T15:04:05Z07:00"} {
			if ts, err := time.Parse(layout, s); err == nil {
				return ts.UTC(), true
			}
		}
		if unix, err := strconv.ParseInt(s, 10, 64); err == nil && unix > 0 {
			return time.Unix(unix, 0).UTC(), true
		}
	case float64:
		if val > 0 {
			return time.Unix(int64(val), 0).UTC(), true
		}
	case int64:
		if val > 0 {
			return time.Unix(val, 0).UTC(), true
		}
	case int:
		if val > 0 {
			return time.Unix(int64(val), 0).UTC(), true
		}
	case json.Number:
		if i, err := val.Int64(); err == nil && i > 0 {
			return time.Unix(i, 0).UTC(), true
		}
	}
	return time.Time{}, false
}

func indexName(table, suffix string) string {
	base := strings.TrimSpace(table)
	if base == "" {
		base = defaultAuthMetaTable
	}
	return base + "_" + suffix + "_idx"
}

func fullQualifiedName(schema, name string) string {
	if strings.TrimSpace(schema) == "" {
		return quoteIdentifier(name)
	}
	return quoteIdentifier(schema) + "." + quoteIdentifier(name)
}
