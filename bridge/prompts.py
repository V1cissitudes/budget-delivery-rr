"""Arm texts for the bridge experiment, verbatim from prereg DRAFT v2 §2 (I0, B1, G, N0, B0). DRAFT: not sealed."""
K = 50

I0 = ("## Budget\n"
      "You have a budget of 50 model calls (each of your responses consumes 1 call).\n"
      "After each command output, a <budget> tag shows used and remaining calls.")

B1 = "<budget>\nCalls Used: {u}, Calls Remaining: {r}\nMake the best use of the available resources.\n</budget>"

G = ("You must ADAPT your strategy dynamically to the current budget state.\n"
     "### HIGH Budget (>=70% remaining)\n"
     "- Explore broadly: inspect the relevant code and reproduce the issue.\n"
     "### MEDIUM Budget (30%-70%)\n"
     "- Converge: implement the fix and close the key gaps efficiently.\n"
     "### LOW Budget (10%-30%)\n"
     "- Verify a single critical behaviour and finalize your changes.\n"
     "### CRITICAL (<10% remaining)\n"
     "- Only run minimal, essential commands.")

N0 = ("## Environment\n"
      "You are working in a sandboxed Linux container with the repository checked out.\n"
      "After each command output, a <status> tag shows the environment status.")

B0 = "<status>\nEnvironment: sandbox ready, repository mounted.\nMake the best use of the available resources.\n</status>"

ARMS = ("silent", "neutral", "number", "bats")


def intro(arm):
    """Text appended to the end of the first user message (None = nothing)."""
    return {"silent": None, "neutral": N0, "number": I0, "bats": I0 + "\n" + G}[arm]


def block(arm, n_calls):
    """Text appended to the end of the last observation of a round, after n_calls model calls have been made."""
    if arm == "silent":
        return None
    if arm == "neutral":
        return B0
    return B1.format(u=n_calls, r=K - n_calls)
