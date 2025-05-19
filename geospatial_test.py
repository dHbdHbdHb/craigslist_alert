import matplotlib.pyplot as plt
from shapely.geometry import Point, Polygon

# 1. Define your neighborhoods as polygons
# (Paste in your GeoJSON polygons; example here uses simple squares for demo)

neighborhood_shapes = {
    "Mission": Polygon([(-122.423, 37.755), (-122.410, 37.755), (-122.410, 37.765), (-122.423, 37.765)]),
    "Noe Valley": Polygon([(-122.440, 37.740), (-122.426, 37.740), (-122.426, 37.750), (-122.440, 37.750)]),
}

# 2. Define some test points (lat, lon)
# Remember: shapely uses (lon, lat) order!
test_points = [
    ("PointA", -122.415, 37.760),  # Should be Mission
    ("PointB", -122.430, 37.745),  # Should be Noe Valley
    ("PointC", -122.418, 37.770),  # Outside both
]

# 3. Assign points to neighborhoods
results = []
for name, lon, lat in test_points:
    pt = Point(lon, lat)
    assigned = None
    for hood, poly in neighborhood_shapes.items():
        if poly.contains(pt):
            assigned = hood
            break
    results.append((name, lon, lat, assigned))

# 4. Print assignments
for r in results:
    print(f"{r[0]} at ({r[1]}, {r[2]}) => {r[3]}")

# 5. Plot for visualization
plt.figure(figsize=(8, 8))
colors = ['red', 'blue', 'green', 'purple', 'orange']
for idx, (hood, poly) in enumerate(neighborhood_shapes.items()):
    x, y = poly.exterior.xy
    plt.fill(x, y, alpha=0.4, fc=colors[idx % len(colors)], label=hood)
# Plot points
for name, lon, lat, assigned in results:
    plt.plot(lon, lat, 'ko' if assigned is None else 'o', markersize=10, label=f"{name}: {assigned or 'None'}")
    plt.text(lon, lat, name, fontsize=9, verticalalignment='bottom')
plt.xlabel("Longitude")
plt.ylabel("Latitude")
plt.legend()
plt.title("Neighborhood Polygons and Test Points")
plt.show()
