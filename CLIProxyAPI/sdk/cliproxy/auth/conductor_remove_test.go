package auth

import (
	"context"
	"testing"
)

type removeTrackingStore struct {
	deleted []string
}

func (s *removeTrackingStore) List(context.Context) ([]*Auth, error) {
	return nil, nil
}

func (s *removeTrackingStore) Save(context.Context, *Auth) (string, error) {
	return "", nil
}

func (s *removeTrackingStore) Delete(_ context.Context, id string) error {
	s.deleted = append(s.deleted, id)
	return nil
}

func TestManager_MarkResultUnauthorizedRemovesAuth(t *testing.T) {
	store := &removeTrackingStore{}
	manager := NewManager(store, nil, nil)

	auth := &Auth{
		ID:       "unauthorized-auth.json",
		FileName: "unauthorized-auth.json",
		Provider: "codex",
		Metadata: map[string]any{"access_token": "x"},
	}
	if _, err := manager.Register(context.Background(), auth); err != nil {
		t.Fatalf("register auth: %v", err)
	}

	manager.MarkResult(context.Background(), Result{
		AuthID:   auth.ID,
		Provider: auth.Provider,
		Model:    "gpt-5-codex",
		Success:  false,
		Error: &Error{
			Message:    "unauthorized",
			HTTPStatus: 401,
		},
	})

	if _, ok := manager.GetByID(auth.ID); ok {
		t.Fatalf("expected auth to be removed after 401")
	}
	if len(store.deleted) != 1 || store.deleted[0] != auth.ID {
		t.Fatalf("expected store delete for %q, got %#v", auth.ID, store.deleted)
	}
}
