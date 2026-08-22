package metrics

import (
	"fmt"
	"testing"
)

func TestRecentSharesRemainBounded(t *testing.T) {
	s := NewStats()
	s.InitCoin("BCH")
	for i := 0; i < maxRecentShares+25; i++ {
		s.RecordShare("BCH", ShareValid, "worker", float64(i))
	}
	shares := s.GetRecentShares("BCH", maxRecentShares)
	if len(shares) != maxRecentShares {
		t.Fatalf("got %d shares, want %d", len(shares), maxRecentShares)
	}
	if shares[0].Difficulty != 25 {
		t.Fatalf("oldest retained difficulty = %v, want 25", shares[0].Difficulty)
	}
}

func TestWorkerStatsRemainBounded(t *testing.T) {
	s := NewStats()
	s.InitCoin("BCH")
	for i := 0; i < maxTrackedWorkers+40; i++ {
		s.RecordShare("BCH", ShareValid, fmt.Sprintf("worker-%d", i), 1)
	}
	workers := s.GetWorkerStats("BCH")
	if len(workers) != maxTrackedWorkers {
		t.Fatalf("got %d workers, want %d", len(workers), maxTrackedWorkers)
	}
}
