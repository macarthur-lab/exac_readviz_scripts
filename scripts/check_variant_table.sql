select v1.chrom, v1.c as v1_count, v2.c as v2_count, v2.c - v1.c as diff from (select chrom, count(*) as c from variant group by chrom) as v2 join (select chrom, count(*) as c from previous_2016_03_27__variant group by chrom) as v1 on v1.chrom=v2.chrom;

select * from variant where chrom not in ('X', 'Y') and variant_id not in (select variant_id from previous_2016_03_27__variant)
