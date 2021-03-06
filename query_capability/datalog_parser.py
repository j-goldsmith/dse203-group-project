import sys
import re
from sqlalchemy import create_engine
from functools import reduce
import logging, pprint
from utils import *

__all__ = ['DatalogParser']

logger = logging.getLogger('qe.DatalogParser')

class DatalogParser:
    def __init__(self, q):
        self.validate(q)
        self._result = q['result']          # Ans(numunits, firstname, billdate)
        self.conditions = q.get('condition', [])    # ['orders.orderid > 1000', 'orders.numunits > 1']

        tables = q['table'] # ['orders(numunits, customerid, orderid)', 'customers(firstname, customerid)', 'orderlines(billdate, orderid)']

        self.source_tables = {}
        self.table_columns = {}
        self.table_column_idx = {}
        self.column_to_table = {}
        self.resolveSourceTables(tables)
        self.return_table = self.getReturnTable(self._result)
        self.join_columns = self.getJoinColumns(self.table_columns)
        self.table_conditions = self.getTableConditions(self.conditions)
        self.resolveNegation(tables)

        self.groupby = None
        self.aggregation = self.resolveAggregation(q.get('groupby'))
        self.join_path = self.resolveJoinPath(self.join_columns, self.source_tables)
        self.return_columns = self.getReturnColumns(self._result)
        self.query_columns = self.getQueryColumns(self.return_columns)         #numunits, firstname, billdate
        self.orderby = q.get('orderby', None)
        self.limit = q.get('limit', None)

        logger.debug("parser structure:\n{}\n".format(pprint.pformat(self.__dict__)))

    def validate(self, datalog):
        support_fields = set(['result', 'table', 'condition', 'groupby', 'orderby', 'limit'])
        fields = set(datalog.keys())
        if not fields.issubset(support_fields):
            fatal("datalog doesn't support fields: {}".format(fields - support_fields))

        if 'result' not in datalog:
            fatal("datalog must has field 'result'")

        if 'table' not in datalog:
            fatal("datalog must has field 'table'")

        if type(datalog['table']) is not list:
            fatal("datalog field 'table' must be a list")

        if 'condition' in datalog and type(datalog['condition']) is not list:
            fatal("datalog field 'condition' must be a list")

        groupby = datalog.get('groupby', None)
        if groupby:
            if 'key' not in groupby or not set(groupby.keys()).issubset(set(['key','aggregation'])):
                fatal("groupby must be dict of {key: ..., aggregation: ...}")

        return True
       #for col in self.return_columns:
       #    if col not in self.column_to_table:
       #        raise Exception("return column '{}' in header doesn't exist in body!".format(col))


    def single_source(self):
        if len(self.source_tables) == 1: return True
        return False

    def single_source_view(self):
        sources = set(self.source_tables.keys())
        if len(sources) == 2 and 'view' in sources: return True
        return False

    def resolveAggregation(self, groupby):
        ''' 'groupby': { 'key': 'pid', 'aggregation': ['count(oid, total_orders)', 'sum(price, total_value)']},'''
        aggregation = {}
        if not groupby: return aggregation
        groupkey,aggs = groupby['key'], groupby.get('aggregation', {})
        col1 = groupkey.split(',')[0]
        source,table = list(self.column_to_table[col1].items())[0]
        self.groupby = {'source': source, 'table':table, 'column': groupkey}

        for agg in aggs:
            match = re.search("(\S+)\((\S+),(\S+)\)", re.sub("\s", '', agg))
            if match:
                func,src_col,tgt_col = match.group(1), match.group(2), match.group(3)
                aggregation[tgt_col] = (func, src_col)

        return aggregation

    def resolveSourceTables(self, tables):
        self.source_tables = {}
        self.column_to_table = {}
        for table in tables:
            if re.search("not\s+(\w+)\.(\w+)\((.*)\)", table): continue # negation

            match = re.search("(\w+)\.(\w+)\((.*)\)", table)
            if not match:
                fatal("datalog table '{}' must follow pattern <source>.<table>(col1, col2, ...)".format(table))
            source, tablename, str_columns = match.group(1), match.group(2), match.group(3)
            self.table_column_idx[tablename] = {}
            if source not in self.source_tables: self.source_tables[source] = []
            self.source_tables[source].append(tablename)
            self.table_columns[tablename] = re.sub('\s','', str_columns).split(',')
            for idx,col in enumerate(self.table_columns[tablename]):
                self.table_column_idx[tablename][col] = idx
                if col not in self.column_to_table: self.column_to_table[col] = {}
                self.column_to_table[col][source] = tablename


    def getReturnTable(self, datalog):
        return datalog[:datalog.index("(")]

    def resolveJoinPath(self, join_columns, source_tables):
        join_paths = {}
        join_paths['MULTI_SOURCE'] = {}
        for col,tables in join_columns.items():
            col_source = {}
            for table in tables:
                source = [s for s,ts in source_tables.items() if table in ts][0]
                if source not in col_source: col_source[source] = []
                col_source[source].append(source+'.'+table)
            if len(col_source) == 1:
                source = list(col_source.keys())[0]
                if source not in join_paths: join_paths[source] = {}
                join_paths[source][col] = tables
            else:
                join_paths['MULTI_SOURCE'][col] = [table for tables in col_source.values() for table in tables]
        return join_paths

    def getReturnColumns(self, datalog):
        return [col.strip() for col in datalog[(datalog.index("(")+1): datalog.index(")")].split(",")]

    # Given a datalog query of form - "Ans(numunits, firstname, billdate), orders.orderid > 1000, orders.numunits > 1"
    # this method extracts the column names from it
    def getQueryColumns(self, ret_cols):
        return_columns = {'MULTI_SOURCE': []}
        for source, tables in self.source_tables.items():
            columns = []
            table_columns = []
            for col in ret_cols:
                if col in self.column_to_table:
                    table = self.column_to_table[col].get(source, None)
                    if not table: continue
                    columns.append(col)
                    table_columns.append({'table':table,'column':col,'func':None,'alias':None})
                    return_columns['MULTI_SOURCE'].append({'table':table,'column':col,'func':None,'alias':None})
                elif col in self.aggregation:
                    func,src_col = self.aggregation[col]
                    table = self.column_to_table[src_col].get(source, None)
                    if not table: continue
                    if self.single_source():
                        table_columns.append({'table':table,'column':src_col,'func':func,'alias':col})
                    else:
                        table_columns.append({'table':table,'column':src_col,'func':None,'alias':None})
                        return_columns['MULTI_SOURCE'].append({'table':table,'column':src_col,'func':func,'alias':col})

            for col in self.join_path['MULTI_SOURCE']:
                table = self.column_to_table[col].get(source, None)
                if table and col not in columns:
                    table_columns.append({'table':table,'column':col,'func':None,'alias':None})
            return_columns[source] = table_columns
        return return_columns


    # Dictionary of {numunits:orders, customerid:orders;customers} etc.
    def getJoinColumns(self, table_columns):
        col_map = {}
        for table,columns in table_columns.items():
            for col in columns:
                if col == '_': continue
                if col in col_map:
                    col_map[col].append(table)
                else:
                    col_map[col] = [table]

        return {col: col_map[col] for col in col_map if len(col_map[col]) > 1}

    def getTableConditions(self, conditions):
        cond_map = {}
        if not conditions: return cond_map
        for cond in conditions:
            match = re.search("(\S+)\s*([>=<]+)\s*(.+)", cond)
            if not match:
                fatal("bad condition pattern: {}".format(cond))
            lop, op, rop = match.group(1), match.group(2), match.group(3)
            # HACK: suppose only 1 operand is column, and pure column in condition 
            tables = []
           #match = re.search("(\w+)\.(\w+)", lop)
           #if match: lop = match.group(2)
           #match = re.search("(\w+)\.(\w+)", rop)
           #if match: rop = match.group(2)

            if lop in self.column_to_table:
                source_tables = self.column_to_table[lop]
            else:
                source_tables = self.column_to_table[rop]

            for source,table in source_tables.items():
                if source not in cond_map: cond_map[source] = {}
                if table not in cond_map: cond_map[source][table] = []
                cond_map[source][table].append([lop, op, rop])
        return cond_map

    def resolveNegation(self, tables):
        negation_map = {}
        negation_values = {}
        true_tables = []
        for val in tables:
            match = re.search('not (\S+)\((.*)\)', val)
            if not match:
                true_tables.append(val)
                continue
            table, columns = match.group(1), match.group(2)
            for idx, col in enumerate(columns.split(',')):
                match = re.search('"(.*)"', col)
                if match:
                    negation_map[table+':'+str(idx)] = match.group(1)
                    continue
                match = re.search('(\d+)', col)
                if match:
                    negation_map[table+':'+str(idx)] = match.group(1)

        for key,value in negation_map.items():
            table, idx = key.split(':')
            source,table = table.split('.')
            colname = self.table_columns[table][int(idx)]
            if colname != '_':
                if source not in self.table_conditions: self.table_conditions[source] = { table: [] }
                if table not in self.table_conditions[source]:
                    self.table_conditions[source][table] = []
                self.table_conditions[source][table].append([colname, '!=', value])
            else:
                fatal("negation value must have variable bound in table '{}'".format(table))
                #self.table_columns[tablename] = re.sub('\s','', str_columns).split(',')
                #self.table_column_idx[tablename][col] = idx
                #self.column_to_table[col][source] = tablename

