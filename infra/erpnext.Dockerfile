# ERPNext + India Compliance (GST: CGST/SGST/IGST by place of supply, HSN, GSTIN, GSTR-1, e-invoice).
# Apps and their built assets live in the image; the sites volume only needs `india_compliance` in apps.txt.
#   docker build -f infra/erpnext.Dockerfile -t aimunim/erpnext:v16.52.1-ic infra
ARG ERPNEXT_IMAGE=frappe/erpnext:v16.52.1
FROM ${ERPNEXT_IMAGE}

ARG IC_BRANCH=version-16
USER frappe
WORKDIR /home/frappe/frappe-bench
# At runtime sites/assets is a link to the image's assets dir; build through the same link so the new
# app's bundle lands in /home/frappe/frappe-bench/assets and assets.json, then drop the temporary link.
RUN rm -rf sites/assets && ln -s /home/frappe/frappe-bench/assets sites/assets \
    && bench get-app --branch ${IC_BRANCH} india_compliance https://github.com/resilient-tech/india-compliance.git \
    && git -C apps/india_compliance log -1 --format='india_compliance %H %cs' > india_compliance.version \
    && test -e assets/india_compliance && grep -q india_compliance assets/assets.json \
    && rm sites/assets
