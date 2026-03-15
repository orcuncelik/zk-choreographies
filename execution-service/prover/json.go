package prover

// TimingJson is the JSON form of ProofTiming.
// WitnessMs is time spent in frontend.NewWitness.
// ProofMs is time spent in groth16.Prove.
type TimingJson struct {
	WitnessMs float64 `json:"witnessMs"`
	ProofMs   float64 `json:"proofMs"`
}

// ProofJson is the JSON form of Proof.
// Timing is additive, so readers that only use Value/Input still work.
type ProofJson struct {
	Value  [8]string  `json:"value"`
	Input  []string   `json:"input"`
	Timing TimingJson `json:"timing"`
}

func (proof Proof) ToJson() ProofJson {
	publicInputs := make([]string, len(proof.Input))
	for i, publicInput := range proof.Input {
		publicInputs[i] = publicInput.String()
	}
	return ProofJson{
		Value: [8]string{
			proof.Value[0].String(),
			proof.Value[1].String(),
			proof.Value[2].String(),
			proof.Value[3].String(),
			proof.Value[4].String(),
			proof.Value[5].String(),
			proof.Value[6].String(),
			proof.Value[7].String(),
		},
		Input: publicInputs,
		Timing: TimingJson{
			WitnessMs: proof.Timing.WitnessMs,
			ProofMs:   proof.Timing.ProofMs,
		},
	}
}
