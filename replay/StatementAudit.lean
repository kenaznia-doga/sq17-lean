import Sq17Lean.PackingStatement
import Sq17Lean.EndpointGeometry

/-! s(17) B statement audit: print every definition the final statement unfolds to,
and the axioms of the existence half (endpoint witness), from our own fresh build. -/
set_option pp.proofs true
#print Sq17.HalfPlaneAssembly.PlainPacking
#print Sq17.IsUnitSquare
#print Sq17.sq
#print Sq17.Propagation.containerBox
#print Sq17.EndpointGeometry.side
#print Sq17.EndpointGeometry.t
#print Sq17.EndpointRoot.coordinates
#print axioms Sq17.HalfPlaneAssembly.endpoint_plainPacking
#print axioms Sq17.HalfPlaneAssembly.feasible_iff_plainPacking
