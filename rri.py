import sys
import tensorflow as tf
from tensorflow.python.ops import variable_scope as vs
from cnn import cnn, DynamicMaxPooling
jumper = tf.load_op_library('./jumper.so')


def batch_slice(batch, start, offset, pad_values=None):
    bs = tf.shape(batch)[0]
    max_offset = tf.reduce_max(offset)
    min_last = tf.reduce_min(tf.shape(batch)[1] - start)
    pad_len = tf.reduce_max([max_offset - min_last, 0])
    rank = len(batch.get_shape())
    remain = tf.shape(batch)[2:]
    # padding
    batch_pad = tf.pad(batch, [[0, 0], [0, pad_len]] + [[0, 0] for r in range(rank - 2)], 'CONSTANT',
                       constant_values=pad_values)
    dim_len = tf.shape(batch_pad)[1]
    # gather
    ind_center = start + tf.range(bs) * dim_len
    ind_region = tf.reshape(tf.expand_dims(ind_center, axis=-1) + tf.expand_dims(tf.range(max_offset), axis=0), [-1])
    region = tf.reshape(tf.gather(tf.reshape(batch_pad, tf.concat([[-1], remain], axis=0)), ind_region),
                        tf.concat([[bs, max_offset], remain], axis=0))
    return region


def get_glimpse_location(match_matrix, dq_size, location, glimpse):
    '''
    get next glimpse location (g_t+1) based on last jump location (j_t)
    '''
    if glimpse == 'fix_hard':
        gp_d_position = tf.cast(tf.floor(location[:, 0] + location[:, 2]), dtype=tf.int32)
        gp_d_offset = tf.reduce_min([tf.ones_like(dq_size[:, 0], dtype=tf.int32) * glimpse_fix_size,
                                     dq_size[:, 0] - gp_d_position], axis=0)
        glimpse_location = tf.stack([tf.cast(gp_d_position, dtype=tf.float32),
                                     tf.zeros_like(location[:, 1]),
                                     tf.cast(gp_d_offset, dtype=tf.float32),
                                     tf.cast(dq_size[:, 1], dtype=tf.float32)], axis=1)
    elif glimpse == 'all_next_hard':
        gp_d_position = tf.cast(tf.floor(location[:, 0] + location[:, 2]), dtype=tf.int32)
        gp_d_offset = dq_size[:, 0] - gp_d_position
        glimpse_location = tf.stack([tf.cast(gp_d_position, dtype=tf.float32),
                                     tf.zeros_like(location[:, 1]),
                                     tf.cast(gp_d_offset, dtype=tf.float32),
                                     tf.cast(dq_size[:, 1], dtype=tf.float32)], axis=1)
    else:
        raise NotImplementedError()
    return glimpse_location


def get_jump_location(match_matrix, dq_size, location, jump, **kwargs):
    '''
    get next jump location (j_t+1) based on glimpse location (g_t+1)
    '''
    if jump == 'max_hard':
        max_d_offset = tf.cast(tf.floor(tf.reduce_max(location[:, 2])), dtype=tf.int32)
        # padding
        match_matrix_pad = tf.pad(match_matrix, [[0, 0], [0, max_d_offset], [0, 0]], 'CONSTANT',
                                  constant_values=sys.float_info.min)
        d_len = tf.shape(match_matrix_pad)[1]
        start = tf.cast(tf.floor(location[:, 0]), dtype=tf.int32)
        gp_ind_center = start + tf.range(bs) * d_len
        gp_ind_region = tf.reshape(tf.expand_dims(gp_ind_center, axis=-1) +
                                   tf.expand_dims(tf.range(max_d_offset), axis=0), [-1])
        glimpse_region = tf.reshape(tf.gather(tf.reshape(match_matrix_pad, [-1, max_q_len]), gp_ind_region),
                                    [-1, max_d_offset, max_q_len])
        d_loc = tf.argmax(tf.reduce_max(tf.abs(glimpse_region), axis=2), axis=1) + start
        new_location = tf.stack([tf.cast(d_loc, dtype=tf.float32),
                                 location[:, 1], tf.ones([bs]), location[:, 3]], axis=1)
    elif jump == 'min_density_hard':
        #new_location = jumper.min_density(match_matrix=match_matrix, dq_size=dq_size, location=location,
        #                                  min_density=min_density)
        # there is no need to use multi-thread op, because this is fast and thus not the bottleneck
        new_location = jumper.min_density_multi_cpu(
            match_matrix=match_matrix, dq_size=dq_size, location=location, min_density=kwargs['min_density'],
            min_jump_offset=kwargs['min_jump_offset'], use_ratio=False, only_one=False)
        new_location = tf.stop_gradient(new_location)
    elif jump == 'all':
        new_location = tf.stop_gradient(location)
    elif jump == 'test':
        new_location = location[:, 0] + tf.reduce_min([tf.ones_like(location[:, 1]), location[:, 1]])
        new_location = tf.stack([new_location, location[:, 1], tf.ones([bs]), location[:, 3]], axis=1)
    else:
        raise NotImplementedError()
    return new_location


def get_representation(match_matrix, dq_size, query, query_emb, doc, doc_emb, word_vector, location, \
                       represent, **kwargs):
    '''
    get the representation based on location (j_t+1)
    '''
    bs = tf.shape(query)[0]
    word_vector_dim = word_vector.get_shape().as_list()[1]
    separate = kwargs['separate']
    state_ta = kwargs['state_ta']
    location_ta = kwargs['location_ta']
    doc_repr_ta = kwargs['doc_repr_ta']
    query_repr_ta = kwargs['query_repr_ta']
    time = kwargs['time']
    is_stop = kwargs['is_stop']
    cur_location = location_ta.read(time)
    cur_next_location = location_ta.read(time + 1)
    with vs.variable_scope('ReprCond'):
        # use last representation if the location remains unchanged
        doc_reuse = \
            tf.logical_and(tf.reduce_all(tf.equal(cur_location[:, 0:4:2], cur_next_location[:, 0:4:2])), 
                           tf.greater_equal(time, 1))
        query_reuse = \
            tf.logical_and(tf.reduce_all(tf.equal(cur_location[:, 1:4:2], cur_next_location[:, 1:4:2])), 
                           tf.greater_equal(time, 1))
    if represent == 'sum_hard':
        state_ta = tf.cond(tf.greater(time, 0), lambda: state_ta, lambda: state_ta.write(0, tf.zeros([bs, 1])))
        start = tf.cast(tf.floor(location[:, :2]), dtype=tf.int32)
        end = tf.cast(tf.floor(location[:, :2] + location[:, 2:]), dtype=tf.int32)
        ind = tf.constant(0)
        representation_ta = tf.TensorArray(dtype=tf.float32, size=bs,
                                           name='representation_ta', clear_after_read=False)
        def body(i, m, s, e, r):
            r_i = tf.reduce_sum(m[i][s[i, 0]:e[i, 0], s[i, 1]:e[i, 1]])
            r = r.write(i, tf.reshape(r_i, [1]))
            return i + 1, m, s, e, r
        _, _, _, _, representation_ta = \
            tf.while_loop(lambda i, m, s, e, r: i < bs, body,
                          [ind, match_matrix, start, end, representation_ta],
                          parallel_iterations=1000)
        representation = representation_ta.stack()
    elif represent == 'interaction_copy_hard':
        '''
        This represent method just copy the match_matrix selected by current region to state_ta.
        Must guarantee that the offset of doc is the same for different step/jump. Offset on query
        is not important because we select regions only based on location of doc.
        Otherwise, the TensorArray will raise inconsistent shape exception.
        '''
        start = tf.cast(tf.floor(location[:, :2]), dtype=tf.int32)
        offset = tf.cast(tf.floor(location[:, 2:]), dtype=tf.int32)
        d_start, d_offset = start[:, 0], offset[:, 0]
        local_match_matrix = batch_slice(match_matrix, d_start, d_offset, pad_values=0)
        # initialize the first element of state_ta
        state_ta = tf.cond(tf.greater(time, 0), lambda: state_ta, 
            lambda: state_ta.write(0, tf.zeros_like(local_match_matrix)))
        representation = local_match_matrix
    elif represent == 'interaction_cnn_hard_resize':
        state_ta = tf.cond(tf.greater(time, 0), lambda: state_ta, lambda: state_ta.write(0, tf.zeros([bs, 200])))
        # in this implementation of "interaction_cnn_hard_resize", we don't calculate similarity matrix again
        if 'max_jump_offset' not in kwargs or 'max_jump_offset2' not in kwargs:
            raise ValueError('max_jump_offset and max_jump_offset2 must be set when InterCNN is used')
        max_jump_offset = kwargs['max_jump_offset']
        max_jump_offset2 = kwargs['max_jump_offset2']
        start = tf.cast(tf.floor(location[:, :2]), dtype=tf.int32)
        offset = tf.cast(tf.floor(location[:, 2:]), dtype=tf.int32)
        d_start, d_offset = start[:, 0], offset[:, 0]
        q_start, q_offset = start[:, 1], offset[:, 1]
        d_end = d_start + d_offset - 1
        q_end = q_start + q_offset - 1
        d_start = d_start / dq_size[:, 0]
        d_end = d_end / dq_size[:, 0]
        q_start = q_start / dq_size[:, 1]
        q_end = q_end / dq_size[:, 1]
        local_match_matrix = tf.image.crop_and_resize(
            tf.expand_dims(match_matrix, -1),
            boxes=tf.cast(tf.stack([d_start, q_start, d_end, q_end], axis=-1), dtype=tf.float32),
            box_ind=tf.range(bs),
            crop_size=[max_jump_offset, max_jump_offset2],
            method='bilinear',
            name='local_interaction'
        )
        with vs.variable_scope('InterCNN'):
            inter_repr = cnn(local_match_matrix, 
                architecture=[(5, 5, 1, 8), (max_jump_offset/5, max_jump_offset2/5)], 
                activation='relu',
                dpool_index=None)
            representation = tf.reshape(inter_repr, [bs, -1])
    elif represent in {'rnn_hard', 'cnn_hard', 'interaction_cnn_hard'}:
        if represent in {'rnn_hard', 'cnn_hard'}:
            state_ta = tf.cond(tf.greater(time, 0), lambda: state_ta, lambda: state_ta.write(0, tf.zeros([bs, 1])))
        elif represent in {'interaction_cnn_hard'}:
            state_ta = tf.cond(tf.greater(time, 0), lambda: state_ta, lambda: state_ta.write(0, tf.zeros([bs, 200])))
        start = tf.cast(tf.floor(location[:, :2]), dtype=tf.int32)
        offset = tf.cast(tf.floor(location[:, 2:]), dtype=tf.int32)
        d_start, d_offset = start[:, 0], offset[:, 0]
        q_start, q_offset = start[:, 1], offset[:, 1]
        d_region = batch_slice(doc, d_start, d_offset, pad_values=0)
        q_region = batch_slice(query, q_start, q_offset, pad_values=0)
        d_region = tf.nn.embedding_lookup(word_vector, d_region)
        q_region = tf.nn.embedding_lookup(word_vector, q_region)
        if represent == 'interaction_cnn_hard':
            # this implementation seems to be slow, wo don't use it
            if 'max_jump_offset' not in kwargs or 'max_jump_offset2' not in kwargs:
                raise ValueError('max_jump_offset and max_jump_offset2 must be set when InterCNN is used')
            max_jump_offset = kwargs['max_jump_offset']
            max_jump_offset2 = kwargs['max_jump_offset2']
            local_match_matrix = tf.matmul(d_region, tf.transpose(q_region, [0, 2, 1]))
            local_match_matrix = tf.pad(local_match_matrix, 
                [[0, 0], [0, max_jump_offset-tf.shape(local_match_matrix)[1]], 
                [0, max_jump_offset2-tf.shape(local_match_matrix)[2]]], 'CONSTANT', constant_values=0)
            local_match_matrix.set_shape([None, max_jump_offset, max_jump_offset2])
            local_match_matrix = tf.expand_dims(local_match_matrix, 3)
            with vs.variable_scope('InterCNN'):
                inter_dpool_index = DynamicMaxPooling.dynamic_pooling_index_2d(d_offset, q_offset, 
                    max_jump_offset, max_jump_offset2)
                inter_repr = cnn(local_match_matrix, architecture=[(5, 5, 1, 8), (5, 5)], activation='relu',
                #inter_repr = cnn(local_match_matrix, architecture=[(5, 5, 1, 16), (500, 10), (5, 5, 16, 16), (1, 1), (5, 5, 16, 16), (10, 1), (5, 5, 16, 100), (25, 10)], activation='relu',
                    dpool_index=inter_dpool_index)
                representation = tf.reshape(inter_repr, [bs, -1])
        elif represent == 'rnn_hard':
            #rnn_cell = tf.nn.rnn_cell.BasicRNNCell(kwargs['rnn_size'])
            rnn_cell = tf.nn.rnn_cell.GRUCell(kwargs['rnn_size'])
            initial_state = rnn_cell.zero_state(bs, dtype=tf.float32)
            d_outputs, d_state = tf.nn.dynamic_rnn(rnn_cell, d_region, initial_state=initial_state,
                                                   sequence_length=d_offset, dtype=tf.float32)
            q_outputs, q_state = tf.nn.dynamic_rnn(rnn_cell, q_region, initial_state=initial_state,
                                                   sequence_length=q_offset, dtype=tf.float32)
            representation = tf.reduce_sum(d_state * q_state, axis=1, keep_dims=True)
        elif represent == 'cnn_hard':
            if 'max_jump_offset' not in kwargs:
                raise ValueError('max_jump_offset must be set when CNN is used')
            max_jump_offset = kwargs['max_jump_offset']
            doc_after_pool_size = max_jump_offset
            doc_arch = [[3, word_vector_dim, 4], [doc_after_pool_size]]
            query_arch = [[3, word_vector_dim, 4], [max_jump_offset]]
            #doc_arch, query_arch = [[3, word_vector_dim, 4], [10]], [[3, word_vector_dim, 4], [5]]
            doc_repr_ta = tf.cond(tf.greater(time, 0), lambda: doc_repr_ta, 
                                  lambda: doc_repr_ta.write(0, tf.zeros([bs, 10, doc_arch[-2][-1]])))
            query_repr_ta = tf.cond(tf.greater(time, 0), lambda: query_repr_ta, 
                                    lambda: query_repr_ta.write(0, tf.zeros([bs, 5, query_arch[-2][-1]])))
            def get_doc_repr():
                nonlocal d_region, max_jump_offset, word_vector_dim, separate, d_offset, doc_arch, doc_after_pool_size
                d_region = tf.pad(d_region, [[0, 0], [0, max_jump_offset - tf.shape(d_region)[1]], [0, 0]], 
                                  'CONSTANT', constant_values=0)
                d_region.set_shape([None, max_jump_offset, word_vector_dim])
                with vs.variable_scope('DocCNN' if separate else 'CNN'):
                    doc_dpool_index = DynamicMaxPooling.dynamic_pooling_index_1d(d_offset, max_jump_offset)
                    doc_repr = cnn(d_region, architecture=doc_arch, activation='relu',
                                   dpool_index=doc_dpool_index)
                with vs.variable_scope('LengthOrderAwareMaskPooling'):
                    mask_prob = tf.minimum(tf.ceil(doc_after_pool_size ** 2 / dq_size[:, 0]), doc_after_pool_size) / 50
                    # length-aware mask
                    mask_ber = tf.distributions.Bernoulli(probs=mask_prob)
                    mask = tf.transpose(mask_ber.sample([doc_after_pool_size]), [1, 0])
                    # order-aware pooling
                    #mask_for_zero = tf.cast(tf.expand_dims(tf.range(doc_after_pool_size), axis=0) < \
                    #    (doc_after_pool_size - tf.reduce_sum(mask, axis=1, keep_dims=True)), dtype=tf.int32)
                    #mask = tf.cast(tf.concat([mask, mask_for_zero], axis=1), dtype=tf.bool)
                    #doc_repr = tf.boolean_mask(tf.concat([doc_repr, tf.zeros_like(doc_repr)], axis=1), mask)
                    #doc_repr = tf.reshape(doc_repr, [bs, doc_after_pool_size, doc_arch[-2][-1]])
                    # normal pooling
                    doc_repr = doc_repr * tf.cast(tf.expand_dims(mask, axis=-1), dtype=tf.float32)
                    # pooling
                    doc_repr = tf.layers.max_pooling1d(doc_repr, pool_size=[5], strides=[5],
                                                       padding='SAME', name='pool')
                return doc_repr
            def get_query_repr():
                nonlocal q_region, max_jump_offset, word_vector_dim, separate, q_offset, query_arch
                q_region = tf.pad(q_region, [[0, 0], [0, max_jump_offset - tf.shape(q_region)[1]], [0, 0]],
                                  'CONSTANT', constant_values=0)
                q_region.set_shape([None, max_jump_offset, word_vector_dim])
                with vs.variable_scope('QueryCNN' if separate else 'CNN'):
                    if not separate:
                        vs.get_variable_scope().reuse_variables()
                    query_dpool_index = DynamicMaxPooling.dynamic_pooling_index_1d(q_offset, max_jump_offset)
                    query_repr = cnn(q_region, architecture=query_arch, activation='relu', 
                                     dpool_index=query_dpool_index)
                    query_repr = tf.layers.max_pooling1d(query_repr, pool_size=[10], strides=[10],
                                                         padding='SAME', name='pool')
                return query_repr
            doc_repr = tf.cond(doc_reuse, lambda: doc_repr_ta.read(time), get_doc_repr)
            query_repr = tf.cond(query_reuse, lambda: query_repr_ta.read(time), get_query_repr)
            #doc_repr = tf.cond(tf.constant(False), lambda: doc_repr_ta.read(time), get_doc_repr)
            #query_repr = tf.cond(tf.constant(False), lambda: query_repr_ta.read(time), get_query_repr)
            doc_repr_ta = doc_repr_ta.write(time + 1, tf.where(is_stop, doc_repr_ta.read(time), doc_repr))
            query_repr_ta = query_repr_ta.write(time + 1, tf.where(is_stop, query_repr_ta.read(time), query_repr))
            cnn_final_dim = 10 * doc_arch[-2][-1] + 5 * query_arch[-2][-1]
            #cnn_final_dim = doc_arch[-1][0] * doc_arch[-2][-1] + query_arch[-1][0] * query_arch[-2][-1]
            dq_repr = tf.reshape(tf.concat([doc_repr, query_repr], axis=1), [-1, cnn_final_dim])
            dq_repr = tf.nn.dropout(dq_repr, kwargs['keep_prob'])
            dq_repr = tf.layers.dense(inputs=dq_repr, units=4, activation=tf.nn.relu)
            representation = tf.layers.dense(inputs=dq_repr, units=1, activation=tf.nn.relu)
    elif represent == 'test':
        representation = tf.ones_like(location[:, :1])
    else:
        raise NotImplementedError()
    state_ta = state_ta.write(time + 1, tf.where(is_stop, state_ta.read(time), representation))
    return state_ta, doc_repr_ta, query_repr_ta


def rri(query, doc, dq_size, max_jump_step, word_vector, interaction='dot', glimpse='fix_hard', glimpse_fix_size=None,
        min_density=None, use_ratio=False, min_jump_offset=1, jump='max_hard', represent='sum_hard', separate=False, 
        aggregate='max', rnn_size=None, max_jump_offset=None, max_jump_offset2=None, keep_prob=1.0):
    bs = tf.shape(query)[0]
    max_q_len = tf.shape(query)[1]
    max_d_len = tf.shape(doc)[1]
    word_vector_dim = word_vector.get_shape().as_list()[1]
    with vs.variable_scope('Embed'):
        query_emb = tf.nn.embedding_lookup(word_vector, query)
        doc_emb = tf.nn.embedding_lookup(word_vector, doc)
    with vs.variable_scope('Match'):
        # match_matrix is of shape (batch_size, max_d_len, max_q_len)
        if interaction == 'indicator':
            match_matrix = tf.cast(tf.equal(tf.expand_dims(doc, axis=2), tf.expand_dims(query, axis=1)),
                                   dtype=tf.float32)
        else:
            if interaction == 'dot':
                match_matrix = tf.matmul(doc_emb, tf.transpose(query_emb, [0, 2, 1]))
            elif interaction == 'cosine':
                match_matrix = tf.matmul(doc_emb, tf.transpose(query_emb, [0, 2, 1]))
                match_matrix /= tf.expand_dims(tf.sqrt(tf.reduce_sum(doc_emb * doc_emb, axis=2)), axis=2) * \
                                tf.expand_dims(tf.sqrt(tf.reduce_sum(query_emb * query_emb, axis=2)), axis=1)
        if min_density != None:
            if use_ratio:
                density = tf.reduce_max(match_matrix, 2)
                mean_density = tf.reduce_mean(density, 1)
                max_density = tf.reduce_max(density, 1)
                min_density = (max_density - mean_density) * min_density + mean_density
            else:
                min_density = tf.ones_like(dq_size[:, 0], dtype=tf.float32) * min_density
    with vs.variable_scope('SelectiveJump'):
        location_ta = tf.TensorArray(dtype=tf.float32, size=1, name='location_ta',
                                     clear_after_read=False, dynamic_size=True) # (d_ind,q_ind,d_len,q_len)
        location_ta = location_ta.write(0, tf.zeros([bs, 4])) # start from the top-left corner
        state_ta = tf.TensorArray(dtype=tf.float32, size=1, name='state_ta', clear_after_read=False, 
                                  dynamic_size=True)
        query_repr_ta = tf.TensorArray(dtype=tf.float32, size=1, name='query_repr_ta', clear_after_read=False, 
                                       dynamic_size=True)
        doc_repr_ta = tf.TensorArray(dtype=tf.float32, size=1, name='doc_repr_ta', clear_after_read=False, 
                                     dynamic_size=True)
        step = tf.zeros([bs], dtype=tf.int32)
        total_offset = tf.zeros([bs], dtype=tf.float32)
        is_stop = tf.zeros([bs], dtype=tf.bool)
        time = tf.constant(0)
        def cond(time, is_stop, step, state_ta, doc_repr_ta, query_repr_ta, location_ta, dq_size, total_offset):
            return tf.logical_and(tf.logical_not(tf.reduce_all(is_stop)),
                                  tf.less(time, tf.constant(max_jump_step)))
        def body(time, is_stop, step, state_ta, doc_repr_ta, query_repr_ta, location_ta, dq_size, total_offset):
            cur_location = location_ta.read(time)
            #time = tf.Print(time, [time], message='time:')
            with vs.variable_scope('Glimpse'):
                glimpse_location = get_glimpse_location(match_matrix, dq_size, cur_location, glimpse)
                # stop when the start index overflow
                new_stop = tf.reduce_any(glimpse_location[:, :2] > tf.cast(dq_size - 1, tf.float32), axis=1)
                glimpse_location = tf.where(new_stop, cur_location, glimpse_location)
                is_stop = tf.logical_or(is_stop, new_stop)
            with vs.variable_scope('Jump'):
                new_location = get_jump_location(match_matrix, dq_size, glimpse_location, jump, 
                    min_density=min_density, min_jump_offset=min_jump_offset)
                if max_jump_offset != None:
                    # truncate long document offset
                    new_location = tf.concat([new_location[:, :2],
                                              tf.minimum(new_location[:, 2:3], max_jump_offset), 
                                              new_location[:, 3:]], axis=1)
                if max_jump_offset2 != None:
                    # truncate long query offset
                    new_location = tf.concat([new_location[:, :2],
                                              new_location[:, 2:3], 
                                              tf.minimum(new_location[:, 3:], max_jump_offset2)], 
                                              axis=1)
                # stop when the start index overflow
                new_stop = tf.reduce_any(new_location[:, :2] > tf.cast(dq_size - 1, tf.float32), axis=1)
                is_stop = tf.logical_or(is_stop, new_stop)
                location_ta = location_ta.write(time + 1, tf.where(is_stop, cur_location, new_location))
                # total length to be modeled
                total_offset += tf.where(is_stop, tf.zeros_like(total_offset), new_location[:, 2])
                # actual rnn length (with padding)
                #total_offset += tf.where(is_stop, tf.zeros_like(total_offset), 
                #    tf.ones_like(total_offset) * \
                #    tf.reduce_max(tf.where(is_stop, tf.zeros_like(total_offset), new_location[:, 2])))
            with vs.variable_scope('Represent'):
                cur_next_location = location_ta.read(time + 1)
                # location_one_out is to prevent duplicate time-consuming calculation
                location_one_out = tf.where(is_stop, tf.ones_like(cur_location), cur_next_location)
                state_ta, doc_repr_ta, query_repr_ta = \
                    get_representation(match_matrix, dq_size, query, query_emb, doc, doc_emb, word_vector, \
                                       location_one_out, represent, max_jump_offset=max_jump_offset, \
                                       max_jump_offset2=max_jump_offset2, rnn_size=rnn_size, keep_prob=keep_prob, \
                                       separate=separate, location_ta=location_ta, state_ta=state_ta, doc_repr_ta=doc_repr_ta, \
                                       query_repr_ta=query_repr_ta, time=time, is_stop=is_stop)
            step = step + tf.where(is_stop, tf.zeros([bs], dtype=tf.int32), tf.ones([bs], dtype=tf.int32))
            return time + 1, is_stop, step, state_ta, doc_repr_ta, query_repr_ta, location_ta, dq_size, total_offset
        _, is_stop, step, state_ta, doc_repr_ta, query_repr_ta, location_ta, dq_size, total_offset = \
            tf.while_loop(cond, body, [time, is_stop, step, state_ta, doc_repr_ta, query_repr_ta, 
                          location_ta, dq_size, total_offset], parallel_iterations=1)
    with vs.variable_scope('Aggregate'):
        states = state_ta.stack()
        location = location_ta.stack()
        location = tf.transpose(location, [1, 0 ,2])
        stop_ratio = tf.reduce_mean(tf.cast(is_stop, tf.float32))
        complete_ratio = tf.reduce_mean(tf.reduce_min(
            [(location[:, -1, 0] + location[:, -1, 2]) / tf.cast(dq_size[:, 0], dtype=tf.float32),
             tf.ones([bs], dtype=tf.float32)], axis=0))
        if aggregate == 'max':
            signal = tf.reduce_max(states, 0)
        elif aggregate == 'sum':
            signal = tf.reduce_sum(states, 0) - states[-1] * \
                tf.cast(tf.expand_dims(time - step, axis=-1), dtype=tf.float32)
        elif aggregate == 'interaction_concat':
            '''
            Concatenate all the state (local match matrix) in state_ta (without the first element 
            because it is initialized as zeros). Then apply CNN.
            '''
            infered_max_d_len = max_jump_step * max_jump_offset
            infered_max_q_len = max_jump_offset2
            concat_match_matrix = tf.reshape(tf.transpose(states[1:], 
                tf.concat([[1, 0], tf.range(len(states.get_shape()))[2:]], axis=0)), 
                [bs, -1, max_q_len])
            concat_match_matrix = tf.pad(concat_match_matrix, 
                [[0, 0], [0, infered_max_d_len-tf.shape(concat_match_matrix)[1]], 
                [0, infered_max_q_len-tf.shape(concat_match_matrix)[2]]], 
                'CONSTANT', constant_values=0)
            concat_match_matrix.set_shape([None, infered_max_d_len, infered_max_q_len])
            concat_match_matrix = tf.expand_dims(concat_match_matrix, 3)
            with vs.variable_scope('ConcateCNN'):
                concat_dpool_index = DynamicMaxPooling.dynamic_pooling_index_2d(
                    tf.cast(total_offset, tf.int32), dq_size[:, 1], 
                    infered_max_d_len, infered_max_q_len)
                concat_repr = cnn(concat_match_matrix, architecture=[(5, 5, 1, 8), (5, 5)], 
                    activation='relu', dpool_index=concat_dpool_index)
                signal = tf.reshape(concat_repr, [bs, 200])
        return signal, {'step': step, 'location': location, 'match_matrix': match_matrix, 
                        'complete_ratio': complete_ratio, 'is_stop': is_stop, 'stop_ratio': stop_ratio,
                        'doc_emb': doc_emb, 'total_offset': total_offset}