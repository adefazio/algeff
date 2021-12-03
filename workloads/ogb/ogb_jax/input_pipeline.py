# Forked from Flax example which can be found here:
# https://github.com/google/flax/blob/main/examples/ogbg_molpcba/input_pipeline.py

"""Exposes the ogbg-molpcba dataset in a convenient format."""

import functools
from typing import Dict, NamedTuple
import jax
import jraph
import numpy as np
import tensorflow as tf
# Hide any GPUs from TensorFlow. Otherwise TF might reserve memory and make
# it unavailable to JAX.
tf.config.experimental.set_visible_devices([], 'GPU')
import tensorflow_datasets as tfds


class GraphsTupleSize(NamedTuple):
  """Helper class to represent padding and graph sizes."""
  n_node: int
  n_edge: int
  n_graph: int


def get_raw_datasets() -> Dict[str, tf.data.Dataset]:
  """Returns datasets as tf.data.Dataset, organized by split."""
  ds_builder = tfds.builder('ogbg_molpcba')
  ds_builder.download_and_prepare()
  ds_splits = ['train', 'validation', 'test']
  datasets = {
      split: ds_builder.as_dataset(split=split) for split in ds_splits
  }
  return datasets


def _get_valid_mask(graphs: jraph.GraphsTuple):
  """Gets the binary mask indicating only valid labels and graphs."""
  labels = graphs.globals
  # We have to ignore all NaN values - which indicate labels for which
  # the current graphs have no label.
  labels_masks = ~np.isnan(labels)

  # Since we have extra 'dummy' graphs in our batch due to padding, we want
  # to mask out any loss associated with the dummy graphs.
  # Since we padded with `pad_with_graphs` we can recover the mask by using
  # get_graph_padding_mask.
  graph_masks = jraph.get_graph_padding_mask(graphs)

  # Combine the mask over labels with the mask over graphs.
  masks = labels_masks & graph_masks[:, None]
  graphs = graphs._replace(globals=[])
  return graphs, labels, masks


def _batch_for_pmap(iterator):
  graphs = []
  labels = []
  masks = []
  count = 0
  for graph_batch, label_batch, mask_batch in iterator:
    count += 1
    graphs.append(graph_batch)
    labels.append(label_batch)
    masks.append(mask_batch)
    if count == jax.local_device_count():
      graphs = jax.tree_multimap(lambda *x: np.stack(x, axis=0), *graphs)
      labels = np.stack(labels)
      masks = np.stack(masks)
      yield graphs, labels, masks
      graphs = []
      labels = []
      masks = []
      count = 0


def get_dataset_iters(batch_size: int,
                      add_virtual_node: bool = True,
                      add_undirected_edges: bool = True,
                      add_self_loops: bool = True) -> Dict[str, tf.data.Dataset]:
  """Returns datasets of batched GraphsTuples, organized by split."""
  if batch_size <= 1:
    raise ValueError('Batch size must be > 1 to account for padding graphs.')

  # Obtain the original datasets.
  datasets = get_raw_datasets()

  # Construct the GraphsTuple converter function.
  convert_to_graphs_tuple_fn = functools.partial(
      convert_to_graphs_tuple,
      add_virtual_node=add_self_loops,
      add_undirected_edges=add_undirected_edges,
      add_self_loops=add_virtual_node,
  )

  # Process each split separately.
  for split_name in datasets:
    # Convert to GraphsTuple.
    datasets[split_name] = datasets[split_name].map(
        convert_to_graphs_tuple_fn,
        num_parallel_calls=tf.data.AUTOTUNE,
        deterministic=True)

  # Compute the padding budget for the requested batch size.
  budget = estimate_padding_budget_for_batch_size(datasets['train'], batch_size,
                                                  num_estimation_graphs=100)

  # Pad an example graph to see what the output shapes will be.
  # We will use this shape information when creating the tf.data.Dataset.
  example_graph = next(datasets['train'].as_numpy_iterator())
  example_padded_graph = jraph.pad_with_graphs(example_graph, *budget)
  padded_graphs_spec = specs_from_graphs_tuple(example_padded_graph)

  # Process each split separately.
  for split_name, dataset_split in datasets.items():

    # Repeat and shuffle the training split.
    if split_name == 'train':
      dataset_split = dataset_split.shuffle(100, reshuffle_each_iteration=True)
      dataset_split = dataset_split.repeat()
    # We cache the validation and test sets, since these are small.
    else:
      dataset_split = dataset_split.cache()

    # Batch and pad each split. Note that this also converts the graphs to
    # numpy.
    batched_iter = jraph.dynamically_batch(
        graphs_tuple_iterator=iter(dataset_split),
        n_node=budget.n_node,
        n_edge=budget.n_edge,
        n_graph=budget.n_graph)

    # An iterator of Tuple[graph, labels, mask].
    masked_iter = map(_get_valid_mask, batched_iter)

    # An iterator the same as above, but where each element has an extra leading
    # dim of size jax.local_device_count().
    pmapped_iterator = _batch_for_pmap(masked_iter)
    datasets[split_name] = pmapped_iterator
  return datasets


def convert_to_graphs_tuple(graph: Dict[str, tf.Tensor],
                            add_virtual_node: bool,
                            add_undirected_edges: bool,
                            add_self_loops: bool) -> jraph.GraphsTuple:
  """Converts a dictionary of tf.Tensors to a GraphsTuple."""
  num_nodes = tf.squeeze(graph['num_nodes'])
  num_edges = tf.squeeze(graph['num_edges'])
  nodes = graph['node_feat']
  edges = graph['edge_feat']
  edge_feature_dim = edges.shape[-1]
  labels = graph['labels']
  senders = graph['edge_index'][:, 0]
  receivers = graph['edge_index'][:, 1]

  # # Add a virtual node connected to all other nodes.
  # # The feature vectors for the virtual node
  # # and the new edges are set to all zeros.
  # if add_virtual_node:
  #   nodes = tf.concat(
  #       [nodes, tf.zeros_like(nodes[0, None])], axis=0)
  #   senders = tf.concat(
  #       [senders, tf.range(num_nodes)], axis=0)
  #   receivers = tf.concat(
  #       [receivers, tf.fill((num_nodes,), num_nodes + 1)], axis=0)
  #   edges = tf.concat(
  #       [edges, tf.zeros((num_nodes, edge_feature_dim))], axis=0)
  #   num_edges += num_nodes
  #   num_nodes += 1

  # # Make edges undirected, by adding edges with senders and receivers flipped.
  # # The feature vector for the flipped edge is the same as the original edge.
  # if add_undirected_edges:
  #   new_senders = tf.concat([senders, receivers], axis=0)
  #   new_receivers = tf.concat([receivers, senders], axis=0)
  #   edges = tf.concat([edges, edges], axis=0)
  #   senders, receivers = new_senders, new_receivers
  #   num_edges *= 2

  # # Add self-loops for each node.
  # # The feature vectors for the self-loops are set to all zeros.
  # if add_self_loops:
  #   senders = tf.concat([senders, tf.range(num_nodes)], axis=0)
  #   receivers = tf.concat([receivers, tf.range(num_nodes)], axis=0)
  #   edges = tf.concat([edges, tf.zeros((num_nodes, edge_feature_dim))], axis=0)
  #   num_edges += num_nodes

  return jraph.GraphsTuple(
      n_node=tf.expand_dims(num_nodes, 0),
      n_edge=tf.expand_dims(num_edges, 0),
      nodes=nodes,
      edges=edges,
      senders=senders,
      receivers=receivers,
      globals=tf.expand_dims(labels, axis=0),
  )


def estimate_padding_budget_for_batch_size(
    dataset: tf.data.Dataset,
    batch_size: int,
    num_estimation_graphs: int) -> GraphsTupleSize:
  """Estimates the padding budget for a dataset of unbatched GraphsTuples.

  Args:
    dataset: A dataset of unbatched GraphsTuples.
    batch_size: The intended batch size. Note that no batching is performed by
      this function.
    num_estimation_graphs: How many graphs to take from the dataset to estimate
      the distribution of number of nodes and edges per graph.

  Returns:
    padding_budget: The padding budget for batching and padding the graphs
    in this dataset to the given batch size.
  """

  def next_multiple_of_64(val: float):
    """Returns the next multiple of 64 after val."""
    return 64 * (1 + int(val // 64))

  if batch_size <= 1:
    raise ValueError('Batch size must be > 1 to account for padding graphs.')

  total_num_nodes = 0
  total_num_edges = 0
  for graph in dataset.take(num_estimation_graphs).as_numpy_iterator():
    graph_size = get_graphs_tuple_size(graph)
    if graph_size.n_graph != 1:
      raise ValueError('Dataset contains batched GraphTuples.')

    total_num_nodes += graph_size.n_node
    total_num_edges += graph_size.n_edge

  num_nodes_per_graph_estimate = total_num_nodes / num_estimation_graphs
  num_edges_per_graph_estimate = total_num_edges / num_estimation_graphs

  padding_budget = GraphsTupleSize(
      n_node=next_multiple_of_64(num_nodes_per_graph_estimate * batch_size),
      n_edge=next_multiple_of_64(num_edges_per_graph_estimate * batch_size),
      n_graph=batch_size)
  return padding_budget


def specs_from_graphs_tuple(graph: jraph.GraphsTuple):
  """Returns a tf.TensorSpec corresponding to this graph."""

  def get_tensor_spec(array: np.ndarray):
    shape = list(array.shape)
    dtype = array.dtype
    return tf.TensorSpec(shape=shape, dtype=dtype)

  specs = {}
  for field in [
      'nodes', 'edges', 'senders', 'receivers', 'globals', 'n_node', 'n_edge'
  ]:
    field_sample = getattr(graph, field)
    specs[field] = get_tensor_spec(field_sample)
  return jraph.GraphsTuple(**specs)


def get_graphs_tuple_size(graph: jraph.GraphsTuple):
  """Returns the number of nodes, edges and graphs in a GraphsTuple."""
  return GraphsTupleSize(
      n_node=np.sum(graph.n_node),
      n_edge=np.sum(graph.n_edge),
      n_graph=np.shape(graph.n_node)[0])
