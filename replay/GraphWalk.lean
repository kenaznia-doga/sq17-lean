import Lean
import Sq17Lean.Square17Optimality

/-! s(17) B: dependency walk from the final theorem over the elaborated environment.
Mirrors `Lean.CollectAxioms` (type and value of definitions/theorems/opaques, constructors of
inductives). Constants owned by a `Sq17Lean.*` module are expanded and written as nodes; every
other constant is written once as a leaf and not expanded. Output (JSON lines, path from env
S17_GRAPH_OUT): one `node` per project constant reached, one `leaf` per external constant reached,
then one `unreached_native` per project `native_decide` axiom present in the environment but not
reached from the root, and a final `summary`. -/
open Lean

def s17Module (env : Environment) (n : Name) : Option Name :=
  match env.getModuleIdxFor? n with
  | some idx =>
    let m := env.header.moduleNames[idx.toNat]!
    if m.getRoot == `Sq17Lean then some m else none
  | none => none

def s17Kind : ConstantInfo → String
  | .axiomInfo _ => "axiom" | .defnInfo _ => "def" | .thmInfo _ => "theorem"
  | .opaqueInfo _ => "opaque" | .quotInfo _ => "quot" | .inductInfo _ => "inductive"
  | .ctorInfo _ => "ctor" | .recInfo _ => "rec"

def s17Edges : ConstantInfo → Array Name
  | .defnInfo v => (v.type.getUsedConstants ++ v.value.getUsedConstants)
  | .thmInfo v => (v.type.getUsedConstants ++ v.value.getUsedConstants)
  | .opaqueInfo v => (v.type.getUsedConstants ++ v.value.getUsedConstants)
  | .inductInfo v => v.type.getUsedConstants ++ v.ctors.toArray
  | .ctorInfo v => v.type.getUsedConstants
  | .recInfo v => v.type.getUsedConstants
  | .axiomInfo v => v.type.getUsedConstants
  | .quotInfo _ => #[]

def s17IsNative (n : Name) : Bool :=
  n.components.any (· == `_native)

#eval show CoreM Unit from do
  let env ← getEnv
  let some out ← IO.getEnv "S17_GRAPH_OUT" | throwError "S17_GRAPH_OUT is not set"
  let h ← IO.FS.Handle.mk out .write
  let root := `Sq17.square17_optimality
  let mut visited : NameHashSet := {}
  let mut stack : Array Name := #[root]
  let mut nodes : Nat := 0
  let mut leaves : Nat := 0
  let mut natives : Nat := 0
  let mut axioms : Array Name := #[]
  while !stack.isEmpty do
    let n := stack.back!
    stack := stack.pop
    if visited.contains n then continue
    visited := visited.insert n
    let some ci := env.find? n | throwError "constant {n} not in environment"
    match s17Module env n with
    | none =>
      leaves := leaves + 1
      if ci matches .axiomInfo _ then axioms := axioms.push n
      h.putStrLn (Json.mkObj [("t", "leaf"), ("n", toString n), ("k", s17Kind ci)]).compress
    | some m =>
      nodes := nodes + 1
      let es := (s17Edges ci).foldl (fun (acc : Array Name) e => if acc.contains e then acc else acc.push e) #[]
      let mut fields : List (String × Json) :=
        [("t", "node"), ("n", toString n), ("m", toString m), ("k", s17Kind ci),
         ("e", Json.arr (es.map fun e => Json.str (toString e)))]
      if let .axiomInfo v := ci then
        axioms := axioms.push n
        if s17IsNative n then natives := natives + 1
        let ty := toString v.type
        fields := fields ++ [("type", Json.str (if ty.length > 4000 then (ty.take 4000).toString ++ "…" else ty)),
                             ("type_len", toJson ty.length)]
      h.putStrLn (Json.mkObj fields).compress
      for e in es do
        unless visited.contains e do stack := stack.push e
  let mut unreached : Nat := 0
  for (n, ci) in env.constants.map₁.toList do
    if let .axiomInfo v := ci then
      if (s17Module env n).isSome && s17IsNative n && !visited.contains n then
        unreached := unreached + 1
        let ty := toString v.type
        h.putStrLn (Json.mkObj [("t", "unreached_native"), ("n", toString n),
          ("m", toString ((s17Module env n).get!)),
          ("type", Json.str (if ty.length > 4000 then (ty.take 4000).toString ++ "…" else ty))]).compress
  h.putStrLn (Json.mkObj [("t", "summary"), ("root", toString root), ("nodes", toJson nodes), ("leaves", toJson leaves),
    ("native_axioms_reached", toJson natives), ("axioms_reached", Json.arr (axioms.map fun a => Json.str (toString a))),
    ("unreached_natives", toJson unreached)]).compress
  IO.println s!"GRAPH_WALK nodes={nodes} leaves={leaves} native_reached={natives} axioms={axioms.size} unreached_natives={unreached}"
