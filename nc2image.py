import numpy as np
from tqdm import tqdm
import re
from PIL import Image, ImageDraw

def generate_arc_points(x_start, y_start, x_end, y_end, x_center, y_center, clockwise=True, interpolation_distance=0.2):
    """
    Generate interpolated points along an arc for G2/G3 commands.

    Parameters:
        x_start, y_start: Starting coordinates of the arc.
        x_end, y_end: Ending coordinates of the arc.
        x_center, y_center: Center of the arc.
        clockwise: True for G2 (clockwise), False for G3 (counterclockwise).
        interpolation_distance: Desired distance between interpolated points in mm.

    Returns:
        List of tuples (x, y) representing the interpolated points along the arc.
    """
    # Calculate the radius of the arc
    radius = np.sqrt((x_start - x_center)**2 + (y_start - y_center)**2)

    # Calculate start and end angles
    start_angle = np.arctan2(y_start - y_center, x_start - x_center)
    if x_end is None or y_end is None \
    or (x_start == x_end and y_start == y_end):
        end_angle = start_angle - (2 * np.pi if clockwise else -2 * np.pi)
    else:
        end_angle = np.arctan2(y_end - y_center, x_end - x_center)

    # Ensure the angles cover the correct rotation direction
    if clockwise and end_angle > start_angle:
        end_angle -= 2 * np.pi
    elif not clockwise and end_angle < start_angle:
        end_angle += 2 * np.pi

    # Calculate the arc length and number of interpolation steps
    arc_length = abs(end_angle - start_angle) * radius
    num_steps = max(1, int(arc_length / interpolation_distance))

    # Generate interpolated points
    points = []
    for step in range(num_steps + 1):
        t = step / num_steps  # Normalized position along the arc
        angle = start_angle + t * (end_angle - start_angle)
        x = x_center + radius * np.cos(angle)
        y = y_center + radius * np.sin(angle)
        points.append((x, y))

    return points

def parse_gcode(file_name):
    """Parse G-code file and extract synchronized X, Y, Z coordinates, handling both spaced and compact formats."""
    x_coords, y_coords, z_coords = [], [], []
    x = y = z = None  # Initialize coordinates to track the previous values

    # Regular expression for extracting commands and parameters
    gcode_pattern = re.compile(r'([GXYZIJ])([-+]?[0-9]*\.?[0-9]+)')

    with open(file_name, 'r') as file:
        for line in file:
            # Ignore comments
            if line.startswith("("):
                continue

            # Search for all matching commands and parameters
            matches = gcode_pattern.findall(line)

            # Process matches
            command = None
            x_end = y_end = z_end = None  # End points for G2/G3
            i_offset = j_offset = 0.0  # Default offsets for arc center

            for match in matches:
                param, value = match
                value = float(value)  # Convert string to float

                if param == 'G':
                    command = value  # Set the command (e.g., G0, G1, G2, G3)
                elif param == 'X':
                    x_end = value
                elif param == 'Y':
                    y_end = value
                elif param == 'Z':
                    z_end = value
                elif param == 'I':
                    i_offset = value
                elif param == 'J':
                    j_offset = value

            # Handle linear moves (G0 and G1)
            if command in (0, 1):  # G0 and G1 are linear movements
                x = x_end if x_end is not None else x
                y = y_end if y_end is not None else y
                z = z_end if z_end is not None else z
                if x is not None and y is not None and z is not None:
                    x_coords.append(x)
                    y_coords.append(y)
                    z_coords.append(z)

            # Handle arcs (G2 and G3)
            elif command in (2, 3):  # G2 for CW and G3 for CCW
                x = x_end if x_end is not None else x
                y = y_end if y_end is not None else y
                arc_points = generate_arc_points(
                    x, y, x_end, y_end, x + i_offset, y + j_offset,
                    clockwise=(command == 2)
                )
                for point in arc_points:
                    x_coords.append(point[0])
                    y_coords.append(point[1])
                    z_coords.append(z_end if z_end is not None else z)

    return x_coords, y_coords, z_coords

def initialize_tool(tool_diameter_mm, px2mm, tool_length_mm=38):
    """Create a NumPy array to represent the tool's shape, with the bottom at zero."""
    tool_radius_px = int((tool_diameter_mm / 2) * px2mm)
    tool_size = 2 * tool_radius_px + 1
    tool = np.full((tool_size, tool_size), tool_length_mm, dtype=np.float32)  # Default to tool length

    # Populate the cutting area of the tool
    for i in range(tool_size):
        for j in range(tool_size):
            dx = i - tool_radius_px
            dy = j - tool_radius_px
            distance = np.sqrt(dx**2 + dy**2) / px2mm
            if distance <= (tool_diameter_mm / 2):
                # Use radius minus depth for the ball-end profile
                depth = np.sqrt((tool_diameter_mm / 2)**2 - distance**2)
                tool[i, j] = tool_diameter_mm / 2.0 - depth
    return tool

def apply_tool(material, tool, x_px, y_px, z_value):
    """
    Apply the tool array to the material array at the specified position and Z height,
    using efficient NumPy array operations.
    """
    tool_size = tool.shape[0]
    half_tool = tool_size // 2

    # Define the bounds of the sub-array in the material
    x_start = max(0, x_px - half_tool)
    x_end = min(material.shape[0], x_px + half_tool + 1)
    y_start = max(0, y_px - half_tool)
    y_end = min(material.shape[1], y_px + half_tool + 1)

    # Define the bounds of the tool array to match the sub-array
    tool_x_start = half_tool - (x_px - x_start)
    tool_x_end = half_tool + (x_end - x_px)
    tool_y_start = half_tool - (y_px - y_start)
    tool_y_end = half_tool + (y_end - y_px)

    # Extract the sub-array of the material and the corresponding region of the tool
    material_subarray = material[x_start:x_end, y_start:y_end]
    tool_subarray = tool[tool_x_start:tool_x_end, tool_y_start:tool_y_end] + z_value

    # Apply the minimum operation
    material[x_start:x_end, y_start:y_end] = np.minimum(material_subarray, tool_subarray)

def create_material(gcode_file, px2mm, tool_diameter_mm, material_top_height, step_mm, output_file, grid_spacing_mm=10):
    """
    Simulate the cutting process and produce a grayscale image from the material array,
    with user-friendly print statements for key information.
    """
    # Parse the G-code
    x_coords, y_coords, z_coords = parse_gcode(gcode_file)
    print(f"Parsed {len(x_coords)} G-code lines.")  # Number of G-code lines

    # Determine the workspace boundaries
    x_min, x_max = min(x_coords), max(x_coords)
    y_min, y_max = min(y_coords), max(y_coords)
    z_min, z_max = min(z_coords), max(z_coords)

    # Print ranges for debugging
    print(f"X range: {x_min:.2f} to {x_max:.2f}")
    print(f"Y range: {y_min:.2f} to {y_max:.2f}")
    print(f"Z range: {z_min:.2f} to {z_max:.2f}")

    # Define the material array
    width = int((x_max - x_min) * px2mm) + 1
    height = int((y_max - y_min) * px2mm) + 1
    print(f"Image dimensions: {width} pixels wide, {height} pixels tall")

    # Initialize the material array
    material = np.full((width, height), material_top_height, dtype=np.float32)
    print(f"Material initialized with top height: {material_top_height} mm")

    # Initialize the tool
    tool = initialize_tool(tool_diameter_mm, px2mm)
    print(f"Tool diameter: {tool_diameter_mm} mm, radius in pixels: {tool.shape[0] // 2}")

    # Process the G-code paths
    print("Simulating the toolpath...")
    for i in tqdm(range(1, len(x_coords))):  # Progress bar for processing
        x1, y1, z1 = x_coords[i - 1], y_coords[i - 1], z_coords[i - 1]
        x2, y2, z2 = x_coords[i], y_coords[i], z_coords[i]

        # Interpolate along the toolpath
        distance = np.sqrt((x2 - x1)**2 + (y2 - y1)**2)
        steps = max(1, int(distance / step_mm))
        for step in range(steps + 1):
            t = step / steps
            x = x1 + t * (x2 - x1)
            y = y1 + t * (y2 - y1)
            z = z1 + t * (z2 - z1)

            # Convert to pixel coordinates
            x_px = int((x - x_min) * px2mm)
            y_px = int((y - y_min) * px2mm)

            # Apply the tool at this step
            apply_tool(material, tool, x_px, y_px, z)

    # Map the material heights to grayscale
    material_grayscale = 255 * (material - z_min) / (material_top_height - z_min)
    material_grayscale = material_grayscale.clip(0, 255).astype(np.uint8)

    # Rotate the image for correct orientation
    material_grayscale = np.rot90(material_grayscale)
    material_grayscale = Image.fromarray(material_grayscale)

    # Create a new image for the grid (RGBA for transparency)
    grid_image = Image.new("RGBA", material_grayscale.size, (0, 0, 0, 0))  # Transparent background
    draw = ImageDraw.Draw(grid_image)

    # Calculate grid spacing in pixels
    grid_spacing_px = int(grid_spacing_mm * px2mm)

    # Draw the gridlines
    for x in range(0, material_grayscale.width, grid_spacing_px):
        draw.line([(x, 0), (x, material_grayscale.height)], fill=(0, 0, 255, 128), width=5)  # Blue, semi-transparent
    for y in range(0, material_grayscale.height, grid_spacing_px):
        draw.line([(0, y), (material_grayscale.width, y)], fill=(0, 0, 255, 128), width=5)  # Blue, semi-transparent

    grid_spacing_px = int(grid_spacing_mm/10 * px2mm)
    for x in range(0, material_grayscale.width, grid_spacing_px):
        draw.line([(x, 0), (x, material_grayscale.height)], fill=(0, 255, 0, 128), width=1)  # Blue, semi-transparent
    for y in range(0, material_grayscale.height, grid_spacing_px):
        draw.line([(0, y), (material_grayscale.width, y)], fill=(0, 255, 0, 128), width=1)  # Blue, semi-transparent


    # Combine the material and grid images
    combined_image = Image.alpha_composite(
        material_grayscale.convert("RGBA"),  # Convert grayscale to RGBA for blending
        grid_image
    )

    # Save the material as an image
    dpi = px2mm*25.4
    combined_image.convert("RGB").save(output_file, dpi=(dpi,dpi))

    print(f"Image saved to {output_file}")

# Parameters
px2mm = 10  # Pixels per millimeter
tool_diameter_mm = 3.175  # Tool diameter in millimeters
material_top_height = 0.0  # Top of the material (e.g., 0mm)
step_mm = 0.2  # Tool movement step in millimeters
gcode_file = "example.nc"
output_file = "example.jpg"

# Generate the material simulation
create_material(gcode_file, px2mm, tool_diameter_mm, material_top_height, step_mm, output_file)
