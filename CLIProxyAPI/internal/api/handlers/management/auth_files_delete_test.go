package management

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/router-for-me/CLIProxyAPI/v6/internal/config"
	coreauth "github.com/router-for-me/CLIProxyAPI/v6/sdk/cliproxy/auth"
)

func registerTestAuth(t *testing.T, manager *coreauth.Manager, authDir, fileName, provider string) string {
	t.Helper()

	filePath := filepath.Join(authDir, fileName)
	payload := []byte(`{"type":"` + provider + `","email":"` + fileName + `"}`)
	if errWrite := os.WriteFile(filePath, payload, 0o600); errWrite != nil {
		t.Fatalf("failed to write auth file %s: %v", fileName, errWrite)
	}

	record := &coreauth.Auth{
		ID:       provider + "/" + fileName,
		FileName: fileName,
		Provider: provider,
		Status:   coreauth.StatusActive,
		Attributes: map[string]string{
			"path": filePath,
		},
		Metadata: map[string]any{
			"type":  provider,
			"email": fileName,
		},
	}
	if _, errRegister := manager.Register(context.Background(), record); errRegister != nil {
		t.Fatalf("failed to register auth %s: %v", fileName, errRegister)
	}

	return filePath
}

func TestListAuthFiles_PaginatesAndFilters(t *testing.T) {
	t.Setenv("MANAGEMENT_PASSWORD", "")
	gin.SetMode(gin.TestMode)

	authDir := t.TempDir()
	manager := coreauth.NewManager(nil, nil, nil)
	registerTestAuth(t, manager, authDir, "alpha@example.com.json", "codex")
	registerTestAuth(t, manager, authDir, "beta@example.com.json", "codex")
	registerTestAuth(t, manager, authDir, "gamma@example.com.json", "gemini")

	h := NewHandlerWithoutConfigFilePath(&config.Config{AuthDir: authDir}, manager)
	h.tokenStore = &memoryAuthStore{}

	rec := httptest.NewRecorder()
	ctx, _ := gin.CreateTestContext(rec)
	req := httptest.NewRequest(http.MethodGet, "/v0/management/auth-files?page=2&page_size=1&type=codex", nil)
	ctx.Request = req
	h.ListAuthFiles(ctx)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected list status %d, got %d with body %s", http.StatusOK, rec.Code, rec.Body.String())
	}

	var payload map[string]any
	if errUnmarshal := json.Unmarshal(rec.Body.Bytes(), &payload); errUnmarshal != nil {
		t.Fatalf("failed to decode list payload: %v", errUnmarshal)
	}

	if gotTotal, ok := payload["total"].(float64); !ok || int(gotTotal) != 2 {
		t.Fatalf("expected total 2, payload: %#v", payload)
	}
	if gotAllTotal, ok := payload["all_total"].(float64); !ok || int(gotAllTotal) != 3 {
		t.Fatalf("expected all_total 3, payload: %#v", payload)
	}
	if gotPage, ok := payload["page"].(float64); !ok || int(gotPage) != 2 {
		t.Fatalf("expected page 2, payload: %#v", payload)
	}
	if gotPageSize, ok := payload["page_size"].(float64); !ok || int(gotPageSize) != 1 {
		t.Fatalf("expected page_size 1, payload: %#v", payload)
	}
	if gotTotalPages, ok := payload["total_pages"].(float64); !ok || int(gotTotalPages) != 2 {
		t.Fatalf("expected total_pages 2, payload: %#v", payload)
	}

	typeCounts, ok := payload["type_counts"].(map[string]any)
	if !ok {
		t.Fatalf("expected type_counts object, payload: %#v", payload)
	}
	if gotCodex, ok := typeCounts["codex"].(float64); !ok || int(gotCodex) != 2 {
		t.Fatalf("expected codex count 2, payload: %#v", payload)
	}
	if gotGemini, ok := typeCounts["gemini"].(float64); !ok || int(gotGemini) != 1 {
		t.Fatalf("expected gemini count 1, payload: %#v", payload)
	}

	filesRaw, ok := payload["files"].([]any)
	if !ok || len(filesRaw) != 1 {
		t.Fatalf("expected one paginated file entry, payload: %#v", payload)
	}
	first, ok := filesRaw[0].(map[string]any)
	if !ok {
		t.Fatalf("expected object entry, payload: %#v", payload)
	}
	if gotName, _ := first["name"].(string); gotName != "beta@example.com.json" {
		t.Fatalf("expected beta@example.com.json, got %#v", first["name"])
	}
}

func TestDeleteAuthFile_DeleteAllFilteredByType(t *testing.T) {
	t.Setenv("MANAGEMENT_PASSWORD", "")
	gin.SetMode(gin.TestMode)

	authDir := t.TempDir()
	manager := coreauth.NewManager(nil, nil, nil)
	codexOnePath := registerTestAuth(t, manager, authDir, "codex-one.json", "codex")
	codexTwoPath := registerTestAuth(t, manager, authDir, "codex-two.json", "codex")
	geminiPath := registerTestAuth(t, manager, authDir, "gemini-one.json", "gemini")

	h := NewHandlerWithoutConfigFilePath(&config.Config{AuthDir: authDir}, manager)
	h.tokenStore = &memoryAuthStore{}

	deleteRec := httptest.NewRecorder()
	deleteCtx, _ := gin.CreateTestContext(deleteRec)
	deleteReq := httptest.NewRequest(http.MethodDelete, "/v0/management/auth-files?all=true&type=codex", nil)
	deleteCtx.Request = deleteReq
	h.DeleteAuthFile(deleteCtx)

	if deleteRec.Code != http.StatusOK {
		t.Fatalf("expected delete status %d, got %d with body %s", http.StatusOK, deleteRec.Code, deleteRec.Body.String())
	}

	var payload map[string]any
	if errUnmarshal := json.Unmarshal(deleteRec.Body.Bytes(), &payload); errUnmarshal != nil {
		t.Fatalf("failed to decode delete payload: %v", errUnmarshal)
	}
	if gotDeleted, ok := payload["deleted"].(float64); !ok || int(gotDeleted) != 2 {
		t.Fatalf("expected 2 deleted files, payload: %#v", payload)
	}

	if _, errStat := os.Stat(codexOnePath); !os.IsNotExist(errStat) {
		t.Fatalf("expected first codex file to be removed, stat err: %v", errStat)
	}
	if _, errStat := os.Stat(codexTwoPath); !os.IsNotExist(errStat) {
		t.Fatalf("expected second codex file to be removed, stat err: %v", errStat)
	}
	if _, errStat := os.Stat(geminiPath); errStat != nil {
		t.Fatalf("expected gemini file to remain, stat err: %v", errStat)
	}
}

func TestDeleteAuthFile_UsesAuthPathFromManager(t *testing.T) {
	t.Setenv("MANAGEMENT_PASSWORD", "")
	gin.SetMode(gin.TestMode)

	tempDir := t.TempDir()
	authDir := filepath.Join(tempDir, "auth")
	externalDir := filepath.Join(tempDir, "external")
	if errMkdirAuth := os.MkdirAll(authDir, 0o700); errMkdirAuth != nil {
		t.Fatalf("failed to create auth dir: %v", errMkdirAuth)
	}
	if errMkdirExternal := os.MkdirAll(externalDir, 0o700); errMkdirExternal != nil {
		t.Fatalf("failed to create external dir: %v", errMkdirExternal)
	}

	fileName := "codex-user@example.com-plus.json"
	shadowPath := filepath.Join(authDir, fileName)
	realPath := filepath.Join(externalDir, fileName)
	if errWriteShadow := os.WriteFile(shadowPath, []byte(`{"type":"codex","email":"shadow@example.com"}`), 0o600); errWriteShadow != nil {
		t.Fatalf("failed to write shadow file: %v", errWriteShadow)
	}
	if errWriteReal := os.WriteFile(realPath, []byte(`{"type":"codex","email":"real@example.com"}`), 0o600); errWriteReal != nil {
		t.Fatalf("failed to write real file: %v", errWriteReal)
	}

	manager := coreauth.NewManager(nil, nil, nil)
	record := &coreauth.Auth{
		ID:          "legacy/" + fileName,
		FileName:    fileName,
		Provider:    "codex",
		Status:      coreauth.StatusError,
		Unavailable: true,
		Attributes: map[string]string{
			"path": realPath,
		},
		Metadata: map[string]any{
			"type":  "codex",
			"email": "real@example.com",
		},
	}
	if _, errRegister := manager.Register(context.Background(), record); errRegister != nil {
		t.Fatalf("failed to register auth record: %v", errRegister)
	}

	h := NewHandlerWithoutConfigFilePath(&config.Config{AuthDir: authDir}, manager)
	h.tokenStore = &memoryAuthStore{}

	deleteRec := httptest.NewRecorder()
	deleteCtx, _ := gin.CreateTestContext(deleteRec)
	deleteReq := httptest.NewRequest(http.MethodDelete, "/v0/management/auth-files?name="+url.QueryEscape(fileName), nil)
	deleteCtx.Request = deleteReq
	h.DeleteAuthFile(deleteCtx)

	if deleteRec.Code != http.StatusOK {
		t.Fatalf("expected delete status %d, got %d with body %s", http.StatusOK, deleteRec.Code, deleteRec.Body.String())
	}
	if _, errStatReal := os.Stat(realPath); !os.IsNotExist(errStatReal) {
		t.Fatalf("expected managed auth file to be removed, stat err: %v", errStatReal)
	}
	if _, errStatShadow := os.Stat(shadowPath); errStatShadow != nil {
		t.Fatalf("expected shadow auth file to remain, stat err: %v", errStatShadow)
	}

	listRec := httptest.NewRecorder()
	listCtx, _ := gin.CreateTestContext(listRec)
	listReq := httptest.NewRequest(http.MethodGet, "/v0/management/auth-files", nil)
	listCtx.Request = listReq
	h.ListAuthFiles(listCtx)

	if listRec.Code != http.StatusOK {
		t.Fatalf("expected list status %d, got %d with body %s", http.StatusOK, listRec.Code, listRec.Body.String())
	}
	var listPayload map[string]any
	if errUnmarshal := json.Unmarshal(listRec.Body.Bytes(), &listPayload); errUnmarshal != nil {
		t.Fatalf("failed to decode list payload: %v", errUnmarshal)
	}
	filesRaw, ok := listPayload["files"].([]any)
	if !ok {
		t.Fatalf("expected files array, payload: %#v", listPayload)
	}
	if len(filesRaw) != 0 {
		t.Fatalf("expected removed auth to be hidden from list, got %d entries", len(filesRaw))
	}
}

func TestDeleteAuthFile_FallbackToAuthDirPath(t *testing.T) {
	t.Setenv("MANAGEMENT_PASSWORD", "")
	gin.SetMode(gin.TestMode)

	authDir := t.TempDir()
	fileName := "fallback-user.json"
	filePath := filepath.Join(authDir, fileName)
	if errWrite := os.WriteFile(filePath, []byte(`{"type":"codex"}`), 0o600); errWrite != nil {
		t.Fatalf("failed to write auth file: %v", errWrite)
	}

	manager := coreauth.NewManager(nil, nil, nil)
	h := NewHandlerWithoutConfigFilePath(&config.Config{AuthDir: authDir}, manager)
	h.tokenStore = &memoryAuthStore{}

	deleteRec := httptest.NewRecorder()
	deleteCtx, _ := gin.CreateTestContext(deleteRec)
	deleteReq := httptest.NewRequest(http.MethodDelete, "/v0/management/auth-files?name="+url.QueryEscape(fileName), nil)
	deleteCtx.Request = deleteReq
	h.DeleteAuthFile(deleteCtx)

	if deleteRec.Code != http.StatusOK {
		t.Fatalf("expected delete status %d, got %d with body %s", http.StatusOK, deleteRec.Code, deleteRec.Body.String())
	}
	if _, errStat := os.Stat(filePath); !os.IsNotExist(errStat) {
		t.Fatalf("expected auth file to be removed from auth dir, stat err: %v", errStat)
	}
}
