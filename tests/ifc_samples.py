"""Synthetic IFC4 houses for the tests (and the sample files): two storeys, slab, walls, two doors, named spaces."""
import math
import numpy as np


def make_house(path, E=0.0, N=0.0, rot=0.0, ground=0.0, georef=True, unit="METERS", empty=False):
    import ifcopenshell.api as api
    import ifcopenshell.api.project, ifcopenshell.api.root, ifcopenshell.api.unit, ifcopenshell.api.context
    import ifcopenshell.api.georeference, ifcopenshell.api.aggregate, ifcopenshell.api.geometry, ifcopenshell.api.spatial
    from ifcopenshell.util.shape_builder import ShapeBuilder
    k = 1000.0 if unit == "MILLIMETERS" else 1.0                   # lengths written in the file's unit
    L, Wd, FFL_UP, STOREY = 18.0, 12.0, 0.15, 3.0
    m = api.project.create_file(version="IFC4")
    proj = api.root.create_entity(m, ifc_class="IfcProject", name="Flood Sandbox test house")
    api.unit.assign_unit(m, length={"is_metric": True, "raw": unit})
    ctx = api.context.add_context(m, context_type="Model")
    body = api.context.add_context(m, context_type="Model", context_identifier="Body", target_view="MODEL_VIEW", parent=ctx)
    if georef:
        api.georeference.add_georeferencing(m)
        api.georeference.edit_georeferencing(m, projected_crs={"Name": "EPSG:27700"}, coordinate_operation={
            "Eastings": E, "Northings": N, "OrthogonalHeight": 0.0, "XAxisAbscissa": math.cos(math.radians(rot)),
            "XAxisOrdinate": math.sin(math.radians(rot)), "Scale": 1.0 / k})
    site = api.root.create_entity(m, ifc_class="IfcSite", name="Site")
    bld = api.root.create_entity(m, ifc_class="IfcBuilding", name="Test house")
    api.aggregate.assign_object(m, relating_object=proj, products=[site]); api.aggregate.assign_object(m, relating_object=site, products=[bld])
    if empty:
        m.write(str(path)); return path
    ffl = ground + FFL_UP
    gf = api.root.create_entity(m, ifc_class="IfcBuildingStorey", name="Ground Floor"); gf.Elevation = ffl * k
    ff = api.root.create_entity(m, ifc_class="IfcBuildingStorey", name="First Floor"); ff.Elevation = (ffl + STOREY) * k
    api.aggregate.assign_object(m, relating_object=bld, products=[gf, ff])
    sb = ShapeBuilder(m)
    def place(el, x, y, z):
        mx = np.eye(4); mx[:3, 3] = [x, y, z]
        api.geometry.edit_object_placement(m, product=el, matrix=mx)  # metres (converted by the API)
    def box(el, sx, sy, sz, storey=None, agg=None):
        rep = sb.get_representation(body, [sb.extrude(sb.rectangle(size=(sx * k, sy * k)), sz * k)])
        api.geometry.assign_representation(m, product=el, representation=rep)
        if storey: api.spatial.assign_container(m, relating_structure=storey, products=[el])
        if agg: api.aggregate.assign_object(m, relating_object=agg, products=[el])
    for z0, st in ((ffl, gf), (ffl + STOREY, ff)):
        s_ = api.root.create_entity(m, ifc_class="IfcSlab", name=f"Slab {st.Name}"); s_.PredefinedType = "FLOOR"
        place(s_, 0, 0, z0 - 0.3); box(s_, L, Wd, 0.3, st)
        for n, (x, y, sx, sy) in enumerate([(0, 0, L, 0.3), (0, Wd - 0.3, L, 0.3), (0, 0, 0.3, Wd), (L - 0.3, 0, 0.3, Wd)]):
            w = api.root.create_entity(m, ifc_class="IfcWall", name=f"Wall {n + 1}"); place(w, x, y, z0); box(w, sx, sy, STOREY, st)
    for nm, x, y in (("Front door", 6.0, -0.05), ("Back door", 11.0, Wd - 0.25)):
        d = api.root.create_entity(m, ifc_class="IfcDoor", name=nm); d.OverallHeight = 2.1 * k; d.OverallWidth = 0.9 * k
        place(d, x, y, ffl); box(d, 0.9, 0.3, 2.1, gf)
    for nm, x, y, w_, d_ in (("Living to hall", 9.15, 8.0, 0.3, 0.9), ("Hall to kitchen", 13.0, 6.15, 0.9, 0.3)):   # internal doors
        d = api.root.create_entity(m, ifc_class="IfcDoor", name=nm); d.OverallHeight = 2.1 * k; d.OverallWidth = 0.9 * k
        place(d, x - w_ / 2, y - d_ / 2, ffl); box(d, w_, d_, 2.1, gf)
    for nm, x, y in (("Living room window", 3.0, -0.05), ("Kitchen window", 14.0, -0.05)):         # windows, sill 0.9 m
        wi = api.root.create_entity(m, ifc_class="IfcWindow", name=nm); wi.OverallHeight = 1.2 * k; wi.OverallWidth = 1.5 * k
        place(wi, x, y, ffl + 0.9); box(wi, 1.5, 0.3, 1.2, gf)
    for nm, use, x, y, sx, sy, st, z0 in (("G01", "Living room", 0.3, 0.3, 9.0, 11.4, gf, ffl), ("G02", "Kitchen", 9.3, 0.3, 8.4, 6.0, gf, ffl),
                                          ("G03", "Hall", 9.3, 6.3, 8.4, 5.4, gf, ffl), ("F01", "Bedroom", 0.3, 0.3, 9.0, 11.4, ff, ffl + STOREY),
                                          ("F02", "Bedroom", 9.3, 0.3, 8.4, 11.4, ff, ffl + STOREY)):
        sp = api.root.create_entity(m, ifc_class="IfcSpace", name=nm); sp.LongName = use
        place(sp, x, y, z0); box(sp, sx, sy, 2.7, agg=st)
    m.write(str(path)); return path


# the test site: a flat plot (24 cm of fall) on a mapped house in Jesmond Vale where the June 2012 storm brings water close
SITE = dict(E=426127.34, N=565794.83, rot=55.6, ground=38.58)
