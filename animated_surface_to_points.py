bl_info = {
	'name': 'Animated Surface to Points',
	'author': 'Ruel Nathaniel Alarcon',
	'version': (0, 1, 0),
	'blender': (3, 6, 0),
	'description': 'Export an animated mesh into 3cpf-format animated point-cloud data',
	'category': 'Mesh',
	'location': 'View3D > Sidebar > Surface to Points Tab'
}

import os
import struct
from functools import partial
from zlib import crc32

import bmesh
import bpy
from mathutils.kdtree import KDTree

try:
	default_file = os.path.join(os.path.expanduser('~'), 'frame_data.3cpf')
except FileNotFoundError as ex:
	print(ex)
	default_file = ''

def local_view(objects=None):
	if objects is None:
		area = [area for area in bpy.context.screen.areas if area.type == "VIEW_3D"][0]
		region = area.regions[-1]

		with bpy.context.temp_override(area=area, region=region):
			bpy.ops.view3d.localview()

		return

	bpy.ops.object.select_all(action='DESELECT')

	for obj in objects:
		if obj.name in bpy.data.objects:
			bpy.data.objects[obj.name].select_set(True)

	bpy.ops.view3d.localview()

def create_bm(mesh):
	bm = bmesh.new()
	bm.from_object(mesh, bpy.context.evaluated_depsgraph_get())
	bm.verts.ensure_lookup_table()
	bm.faces.ensure_lookup_table()
	return bm

def count_vertices(mesh):
	bm = create_bm(mesh)
	count = len(bm.verts)
	bm.free()
	return count

def get_vertex_color(mesh, vertex_index):
	attribute = mesh.data.attributes.get('Color')
	layer = attribute.data
	return layer[vertex_index].color

def get_nearest_vertex_on_face(mesh, face, point):
	transformed_vertices = ((vert, mesh.matrix_world @ vert.co) for vert in face.verts)
	nearest_vertex, _ = min(transformed_vertices, key=lambda v: (v[1] - point).length)
	return nearest_vertex

def export_frame_data_3cpf(mesh_color_pairs, frame_range, file_path):
	vertex_data = bytearray()
	frame_data = bytearray()

	bpy.ops.screen.animation_cancel()

	bpy.context.scene.frame_set(frame_range[0])
	bpy.context.view_layer.update()

	vertex_count = sum(count_vertices(mesh) for (mesh, _) in mesh_color_pairs)
	total_frames = frame_range[1] - frame_range[0] + 1

	visible_modifiers = set()
	global_vertices = {}

	# For each vertex in the point mesh, find the nearest vertex in the color mesh,
	# then use that to store the color, position difference and face rotation
	for point_mesh, color_mesh in mesh_color_pairs:
		bm = create_bm(point_mesh)
		bm_color = create_bm(color_mesh)
		faces = KDTree(len(bm_color.faces))

		for index, face in enumerate(bm_color.faces):
			face_center = point_mesh.matrix_world @ face.calc_center_median()
			faces.insert(face_center, index)

		faces.balance()

		mesh_vertices = []

		for vertex in bm.verts:
			position = point_mesh.matrix_world @ vertex.co

			_, face_index, _ = faces.find(position)
			face = bm_color.faces[face_index]

			color_vertex = get_nearest_vertex_on_face(color_mesh, face, position)

			color = get_vertex_color(color_mesh, color_vertex.index)
			rgb = [int(c * 255) for c in color[:3]]

			position_offset = vertex.co - color_vertex.co
			original_rotation = face.normal.to_track_quat('Z', 'Y').to_euler()

			mesh_vertices.append((color_vertex.index, face_index, position_offset, original_rotation))
			vertex_data += struct.pack('3B', *rgb)

		bm.free()
		bm_color.free()

		for modifier in point_mesh.modifiers:
			if modifier.show_viewport:
				visible_modifiers.add(modifier)
				modifier.show_viewport = False

		global_vertices[point_mesh.name] = mesh_vertices

	local_view([color_mesh for (_, color_mesh) in mesh_color_pairs])

	# Process each frame, tracking the chosen vertices in the color mesh as the position of each point
	# A separate function is used here to be passed into a timer, preventing blender from freezing while processing
	def process(area):
		nonlocal mesh_color_pairs
		nonlocal frame_range
		nonlocal file_path
		nonlocal vertex_data
		nonlocal frame_data
		nonlocal vertex_count
		nonlocal total_frames
		nonlocal visible_modifiers
		nonlocal global_vertices

		if process.frame <= frame_range[1]:
			area.header_text_set(f'Processing frame: {process.frame} / {frame_range[1]}')

			bpy.context.scene.frame_set(process.frame)
			bpy.context.view_layer.update()

			for point_mesh, color_mesh in mesh_color_pairs:
				bm_color = create_bm(color_mesh)

				for vertex_index, face_index, position_offset, original_rotation in global_vertices[point_mesh.name]:
					face = bm_color.faces[face_index]
					vertex = bm_color.verts[vertex_index]

					# The position of each point was not exactly the position of the color mesh vertex, so we
					# used the stored offset and rotation to return it to its true position
					rotation = face.normal.to_track_quat('Z', 'Y').to_euler()
					rotation_diff = rotation.to_quaternion().rotation_difference(original_rotation.to_quaternion())
					rotated_offset = rotation_diff @ position_offset

					position = color_mesh.matrix_world @ (vertex.co + rotated_offset)

					x, y, z = position.x, position.y, position.z

					frame_data += struct.pack('3f', x, y, z)

				bm_color.free()

			process.frame += frame_range[2]
			return 0

		if process.frame <= frame_range[1] + frame_range[2]:
			for modifier in visible_modifiers:
				modifier.show_viewport = True

			local_view()

			checksum = crc32(vertex_data + frame_data) & 0xffffffff

			with open(file_path, 'wb') as file:
				file.write(b'3CPF')
				header = struct.pack('4I', 1, checksum, vertex_count, total_frames)
				file.write(header)

				file.write(vertex_data)
				file.write(frame_data)

				file.close()

			area.header_text_set(f'Successfully exported 3cpf data to {file_path}')

			bpy.context.scene.frame_set(frame_range[0])
			bpy.context.view_layer.update()

			process.frame += frame_range[2]

			bpy.ops.object.select_all(action='DESELECT')

			return 3.0

		area.header_text_set(None)
		return None

	process.frame = frame_range[0]
	bpy.app.timers.register(partial(process, bpy.context.area))

def get_mesh_color_pairs(selected_meshes):
	mesh_color_pairs = []
	for obj in selected_meshes:
		point_mesh_name = obj.name.removesuffix('_points')
		color_mesh_name = point_mesh_name + '_colors'
		color_mesh = bpy.data.objects.get(color_mesh_name)
		mesh_color_pairs.append((obj, color_mesh))
	return mesh_color_pairs

def distribute_vertices(meshes, target_point_amount, frame_range, threshold=10, iterations=16):
	largest_mesh = max(meshes, key=lambda m: max(m.dimensions))

	bpy.context.scene.frame_set(frame_range[0])
	bpy.context.view_layer.update()

	def initialize_point_distribution_nodes(mesh, distance_min=None):
		clone = mesh.copy()
		clone.data = mesh.data.copy()
		clone.name = mesh.name + '_colors'
		mesh.name = mesh.name + '_points'
		bpy.context.collection.objects.link(clone)

		geom_nodes_modifier = mesh.modifiers.get('GeometryNodes') or \
							mesh.modifiers.new(name='GeometryNodes', type='NODES')

		if not geom_nodes_modifier.node_group:
			geom_nodes_modifier.node_group = bpy.data.node_groups.new(name='Geometry Nodes Group', type='GeometryNodeTree')

		node_tree = geom_nodes_modifier.node_group
		node_tree.nodes.clear()

		group_input = node_tree.nodes.new('NodeGroupInput')
		group_output = node_tree.nodes.new('NodeGroupOutput')
		node_tree.inputs.new('NodeSocketGeometry', "Geometry")
		node_tree.outputs.new('NodeSocketGeometry', "Geometry")

		distribute_points_node = node_tree.nodes.new(type='GeometryNodeDistributePointsOnFaces')
		distribute_points_node.distribute_method = 'POISSON'

		points_to_vertices_node = node_tree.nodes.new(type='GeometryNodePointsToVertices')

		node_tree.links.new(group_input.outputs['Geometry'], distribute_points_node.inputs['Mesh'])
		node_tree.links.new(distribute_points_node.outputs['Points'], points_to_vertices_node.inputs['Points'])
		node_tree.links.new(points_to_vertices_node.outputs['Mesh'], group_output.inputs['Geometry'])

		group_input.location = (-400, 0)
		distribute_points_node.location = (-200, 0)
		points_to_vertices_node.location = (0, 0)
		group_output.location = (200, 0)

		if distance_min is not None:
			distribute_points_node.inputs['Distance Min'].default_value = distance_min
			return distribute_points_node

		return distribute_points_node

	distribute_points_node = initialize_point_distribution_nodes(largest_mesh)
	current_point_amount = count_vertices(largest_mesh)

	largest_dimension = max((largest_mesh.dimensions.x, largest_mesh.dimensions.y, largest_mesh.dimensions.z))
	distribute_points_node.inputs['Density Max'].default_value = max(1000, largest_dimension * 10_000)
	distance_min = largest_dimension

	lower_bound = 0.0001
	upper_bound = largest_dimension

	# Due to the distribution of points on a complicated surface, it's difficult to know the exact density and min_distance
	# required for a geometry node to generate an exact amount of points. Instead, we use a binary search algorithm to iterate
	# until we are within the specified threshold of points.
	def process(area):
		nonlocal meshes
		nonlocal target_point_amount
		nonlocal frame_range
		nonlocal threshold
		nonlocal iterations
		nonlocal largest_mesh
		nonlocal distribute_points_node
		nonlocal current_point_amount
		nonlocal largest_dimension
		nonlocal lower_bound
		nonlocal upper_bound
		nonlocal distance_min

		if not process.complete and abs(current_point_amount - target_point_amount) > threshold and process.iteration < iterations:
			distance_min = (lower_bound + upper_bound) / 2
			distribute_points_node.inputs['Distance Min'].default_value = distance_min

			bpy.context.view_layer.update()
			current_point_amount = count_vertices(largest_mesh)

			area.header_text_set(f'Approximating point amount. Iteration: {process.iteration} - Current point amount for control mesh: {current_point_amount}')

			if abs(current_point_amount - target_point_amount) <= threshold:
				return 0

			if current_point_amount > target_point_amount:
				lower_bound = distance_min
			else:
				upper_bound = distance_min

			process.iteration += 1
			return 0

		if not process.complete:
			bpy.ops.object.select_all(action='DESELECT')

			meshes.remove(largest_mesh)
			largest_mesh.select_set(True)

			total_point_amount = current_point_amount

			for mesh in meshes:
				distribute_points_node = initialize_point_distribution_nodes(mesh)
				largest_dimension = max((mesh.dimensions.x, mesh.dimensions.y, mesh.dimensions.z))
				distribute_points_node.inputs['Density Max'].default_value = largest_dimension * 10_000
				distribute_points_node.inputs['Distance Min'].default_value = distance_min

				bpy.context.view_layer.update()
				mesh.select_set(True)
				total_point_amount += count_vertices(mesh)

			area.header_text_set(f'Point distribution complete. Total point count for all selected meshes: {total_point_amount}\n')

			process.complete = True
			return 3.0

		area.header_text_set(None)
		return None

	process.iteration = 0
	process.complete = False
	bpy.app.timers.register(partial(process, bpy.context.area))

def undistribute_vertices(mesh_color_pairs):
	for point_mesh, color_mesh in mesh_color_pairs:
		if color_mesh is None:
			continue

		mesh_name = point_mesh.name.removesuffix('_points')
		bpy.data.objects.remove(point_mesh, do_unlink=True)
		color_mesh.name = mesh_name

def change_framerate(new_framerate):
	scene = bpy.context.scene

	original_fps = scene.render.fps

	ratio = new_framerate / original_fps

	bpy.context.scene.frame_start = int(bpy.context.scene.frame_start * ratio + 0.5)
	bpy.context.scene.frame_end = int(bpy.context.scene.frame_end * ratio + 0.5)

	scene.render.fps = new_framerate

	scene.render.frame_map_new = round(scene.render.frame_map_new * ratio)
	scene.frame_set(int(scene.frame_current * ratio + 0.5))

class PointDistributionProperties(bpy.types.PropertyGroup):
	target_point_amount: bpy.props.IntProperty(
		name='Number of Points',
		description='The approximate number of points you want to distribute on the surface of your mesh',
		default=1000
	)
	approximation_threshold: bpy.props.IntProperty(
		name='Point Threshold',
		description='How close to the approximate number of points is acceptable. Note that even with a threshold of zero, the number of points may still vary by a few points',
		default=10
	)
	approximation_iterations: bpy.props.IntProperty(
		name='Point Iterations',
		description='Number of iterations for approximating the target number of points',
		default=16
	)

class ExportProperties(bpy.types.PropertyGroup):
	file_path: bpy.props.StringProperty(
		name='File',
		description='Name of the file to export',
		default=default_file,
		subtype='FILE_PATH'
	)

class PointDistributionPanel(bpy.types.Panel):
	bl_label = 'Point Distribution'
	bl_idname = 'VIEW3D_PT_PointDistributionPanel'
	bl_space_type = 'VIEW_3D'
	bl_region_type = 'UI'
	bl_category = 'Surface to Points'

	def draw(self, context):
		layout = self.layout
		scene = context.scene
		point_dist_tool = scene.point_dist_tool

		layout.prop(point_dist_tool, 'target_point_amount')
		layout.prop(point_dist_tool, 'approximation_threshold')
		layout.prop(point_dist_tool, 'approximation_iterations')

		layout.operator('point.dist_operator', icon='OUTLINER_OB_POINTCLOUD')
		layout.operator('point.undo_dist_operator', icon='OUTLINER_DATA_POINTCLOUD')

class ExportPanel(bpy.types.Panel):
	bl_label = 'Export'
	bl_idname = 'VIEW3D_PT_ExportPanel'
	bl_space_type = 'VIEW_3D'
	bl_region_type = 'UI'
	bl_category = 'Surface to Points'

	def draw(self, context):
		layout = self.layout
		scene = context.scene
		export_tool = scene.export_tool

		layout.prop(export_tool, 'file_path')
		layout.operator('export.export_operator', icon='EXPORT')

		selected_meshes = [obj for obj in context.selected_objects if obj.type == 'MESH']
		point_meshes = [point_mesh for (point_mesh, color_mesh) in get_mesh_color_pairs(selected_meshes) if color_mesh is not None]

		if len(point_meshes) > 0:
			count = sum(count_vertices(mesh) for mesh in point_meshes)
			layout.label(text=f'Point count in selected: {count}', icon='INFO')
		else:
			layout.label(text='No point meshes selected.', icon='INFO')

class PointDistributionOperator(bpy.types.Operator):
	bl_label = 'Distribute Points'
	bl_idname = 'point.dist_operator'
	bl_description = 'Distributes points on the selected mesh according to specified parameters'

	def execute(self, context):
		scene = context.scene
		point_dist_tool = scene.point_dist_tool

		frame_range = (scene.frame_start, scene.frame_end, scene.frame_step)

		selected_meshes = [obj for obj in bpy.context.selected_objects if obj.type == 'MESH']

		if len(selected_meshes) == 0:
			self.report({'ERROR'}, f'You must have selected the meshes which you wish to distribute points onto.')
			return {'CANCELLED'}

		distribute_vertices(
			selected_meshes,
			point_dist_tool.target_point_amount,
			frame_range,
			point_dist_tool.approximation_threshold,
			point_dist_tool.approximation_iterations
		)
		return {'FINISHED'}

class UndoPointsOperator(bpy.types.Operator):
	bl_label = 'Undo Point Distribution'
	bl_idname = 'point.undo_dist_operator'
	bl_description = 'Undoes point distribution on the selected point meshes'

	def execute(self, context):
		selected_meshes = [obj for obj in bpy.context.selected_objects if obj.type == 'MESH']
		mesh_color_pairs = get_mesh_color_pairs(selected_meshes)

		if len(mesh_color_pairs) == 0:
			self.report({'ERROR'}, f'No point meshes are selected.')
			return {'CANCELLED'}

		undistribute_vertices(mesh_color_pairs)

		return {'FINISHED'}

class ExportOperator(bpy.types.Operator):
	bl_label = 'Export to .3cpf'
	bl_idname = 'export.export_operator'
	bl_description = 'Exports colored animated point data in a custom binary format'

	def execute(self, context):
		scene = context.scene
		export_tool = scene.export_tool

		selected_meshes = [obj for obj in bpy.context.selected_objects if obj.type == 'MESH']
		mesh_color_pairs = get_mesh_color_pairs(selected_meshes)

		if len(mesh_color_pairs) == 0:
			self.report({'ERROR'}, f'No point meshes are selected.')
			return {'CANCELLED'}

		for point_mesh, color_mesh in mesh_color_pairs:
			mesh_name = point_mesh.name.removesuffix('_points')
			if color_mesh is None:
				self.report({'ERROR'}, f"The point mesh '{point_mesh.name}' does not have a corresponding color mesh '{mesh_name}_colors'\nIs it possible you've selected a mesh that is not a point mesh?")
				return {'CANCELLED'}

			if color_mesh.data.attributes.get('Color') is None:
				self.report({'ERROR'}, f"The color mesh {color_mesh.name} must have a Color Attribute named 'Color' for its vertex colors.")
				return {'CANCELLED'}

		frame_range = (scene.frame_start, scene.frame_end, scene.frame_step)

		file_path = export_tool.file_path

		if len(file_path) == 0:
			self.report({'ERROR'}, f'You must first specify a file path.')
			return {'CANCELLED'}

		export_frame_data_3cpf(mesh_color_pairs, frame_range, file_path)

		return {'FINISHED'}

class UtilitiesProperties(bpy.types.PropertyGroup):
	new_framerate: bpy.props.IntProperty(
		name='New Framerate',
		description='New target framerate',
		default=24,
		min=1
	)

class UtilitiesPanel(bpy.types.Panel):
	bl_label = 'Utilities'
	bl_idname = 'VIEW3D_PT_UtilitiesPanel'
	bl_space_type = 'VIEW_3D'
	bl_region_type = 'UI'
	bl_category = 'Surface to Points'
	bl_options = {'DEFAULT_CLOSED'}

	def draw(self, context):
		layout = self.layout
		scene = context.scene
		utilities_tool = scene.utilities_tool

		layout.prop(utilities_tool, 'new_framerate')
		layout.operator('utilities.change_framerate_operator', icon='TIME')

class ChangeFramerateOperator(bpy.types.Operator):
	bl_label = 'Set New Framerate'
	bl_idname = 'utilities.change_framerate_operator'
	bl_description = 'Sets the percieved framerate of the current animation to the new specified framerate value'

	def execute(self, context):
		scene = context.scene
		utilities_tool = scene.utilities_tool
		change_framerate(utilities_tool.new_framerate)
		return {'FINISHED'}

def register():
	bpy.utils.register_class(PointDistributionProperties)
	bpy.types.Scene.point_dist_tool = bpy.props.PointerProperty(type=PointDistributionProperties)

	bpy.utils.register_class(ExportProperties)
	bpy.types.Scene.export_tool = bpy.props.PointerProperty(type=ExportProperties)

	bpy.utils.register_class(UtilitiesProperties)
	bpy.types.Scene.utilities_tool = bpy.props.PointerProperty(type=UtilitiesProperties)

	bpy.utils.register_class(PointDistributionPanel)
	bpy.utils.register_class(ExportPanel)
	bpy.utils.register_class(UtilitiesPanel)

	bpy.utils.register_class(PointDistributionOperator)
	bpy.utils.register_class(UndoPointsOperator)
	bpy.utils.register_class(ExportOperator)
	bpy.utils.register_class(ChangeFramerateOperator)

def unregister():
	bpy.utils.unregister_class(PointDistributionProperties)
	del bpy.types.Scene.point_dist_tool

	bpy.utils.unregister_class(ExportProperties)
	del bpy.types.Scene.export_tool

	bpy.utils.unregister_class(UtilitiesProperties)
	del bpy.types.Scene.utilities_tool

	bpy.utils.unregister_class(PointDistributionPanel)
	bpy.utils.unregister_class(ExportPanel)

	bpy.utils.unregister_class(PointDistributionOperator)
	bpy.utils.unregister_class(UndoPointsOperator)
	bpy.utils.unregister_class(ExportOperator)
	bpy.utils.unregister_class(ChangeFramerateOperator)

if __name__ == '__main__':
	register()