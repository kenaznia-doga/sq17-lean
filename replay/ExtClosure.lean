import Lean
open Lean

/-- Print the full module import closure of the modules named in the given file (one per line). -/
def main (args : List String) : IO Unit := do
  let [file] := args | throw <| IO.userError "usage: ExtClosure FILE"
  initSearchPath (← findSysroot)
  let names := ((← IO.FS.lines file).filter (· ≠ "")).map String.toName
  let env ← importModules (names.map fun n => ({module := n} : Import)) {}
  for n in env.header.moduleNames do
    IO.println n
